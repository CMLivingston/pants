# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os

from pants.backend.python.interpreter_cache import PythonInterpreterCache
from pants.backend.python.python_requirement import PythonRequirement
from pants.backend.python.subsystems.python_repos import PythonRepos
from pants.backend.python.subsystems.python_setup import PythonSetup
from pants.backend.python.targets.python_requirement_library import PythonRequirementLibrary
from pants.backend.python.targets.python_target import PythonTarget
from pants.backend.python.tasks import pex_build_util
from pants.base.build_environment import get_buildroot, pants_version
from pants.base.deprecated import deprecated_conditional
from pants.base.exceptions import TaskError
from pants.base.hash_utils import hash_all
from pants.base.workunit import WorkUnitLabel
from pants.build_graph.address import Address
from pants.option.custom_types import file_option
from pants.task.lint_task_mixin import LintTaskMixin
from pants.task.task import Task
from pants.util.collections import factory_dict
from pants.util.contextutil import temporary_file
from pants.util.dirutil import safe_concurrent_creation
from pants.util.memo import memoized_classproperty, memoized_property
from pex.pex import PEX
from pex.pex_builder import PEXBuilder
from pkg_resources import Distribution, Requirement, RequirementParseError

from pants.contrib.python.checks.checker import checker
from pants.contrib.python.checks.tasks.checkstyle.plugin_subsystem_base import \
  default_subsystem_for_plugin
from pants.contrib.python.checks.tasks.checkstyle.pycodestyle_subsystem import PyCodeStyleSubsystem
from pants.contrib.python.checks.tasks.checkstyle.pyflakes_subsystem import FlakeCheckSubsystem


class Checkstyle(LintTaskMixin, Task):
  _PYTHON_SOURCE_EXTENSION = '.py'

  _CUSTOM_PLUGIN_SUBSYSTEMS = (
    PyCodeStyleSubsystem,
    FlakeCheckSubsystem,
  )

  @memoized_classproperty
  def _python_3_compatible_dists(cls):
    _VERSIONS = ["3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9"]
    _PROJECTS = ["CPython", "stackless", "activepython"]
    _PYTHON_3_DISTS = []
    for proj in _PROJECTS:
      for ver in _VERSIONS:
        _PYTHON_3_DISTS.append(Distribution(project_name=proj, version=ver))
    return _PYTHON_3_DISTS

  @memoized_classproperty
  def plugin_subsystems(cls):
    subsystem_type_by_plugin_type = factory_dict(default_subsystem_for_plugin)
    subsystem_type_by_plugin_type.update((subsystem_type.plugin_type(), subsystem_type)
                                         for subsystem_type in cls._CUSTOM_PLUGIN_SUBSYSTEMS)
    return tuple(subsystem_type_by_plugin_type[plugin_type] for plugin_type in checker.plugins())

  @classmethod
  def subsystem_dependencies(cls):
    return super(Task, cls).subsystem_dependencies() + cls.plugin_subsystems + (
      # Needed implicitly by the pex_build_util functions we use.
      PythonSetup, PythonRepos)

  @classmethod
  def register_options(cls, register):
    super(Checkstyle, cls).register_options(register)
    register('--severity', fingerprint=True, default='COMMENT', type=str,
             help='Only messages at this severity or higher are logged. [COMMENT WARNING ERROR].')
    register('--strict', fingerprint=True, type=bool,
             help='If enabled, have non-zero exit status for any nit at WARNING or higher.')
    register('--suppress', fingerprint=True, type=file_option, default=None,
             help='Takes a text file where specific rules on specific files will be skipped.')
    register('--fail', fingerprint=True, default=True, type=bool,
             help='Prevent test failure but still produce output for problems.')
    register('--enable-py3-lint', fingerprint=True, default=False, type=bool,
             help='Enable linting on Python 3-compatible targets.')

  def _is_checked(self, target):
    return (not target.is_synthetic and isinstance(target, PythonTarget) and
            target.has_sources(self._PYTHON_SOURCE_EXTENSION))

  _CHECKER_ADDRESS_SPEC = 'contrib/python/src/python/pants/contrib/python/checks/checker'
  _CHECKER_REQ = 'pantsbuild.pants.contrib.python.checks.checker=={}'.format(pants_version())
  _CHECKER_ENTRYPOINT = 'pants.contrib.python.checks.checker.checker:main'

  @memoized_property
  def checker_target(self):
    self.context.resolve(self._CHECKER_ADDRESS_SPEC)
    return self.context.build_graph.get_target(Address.parse(self._CHECKER_ADDRESS_SPEC))

  def checker_pex(self, interpreter):
    # TODO(John Sirois): Formalize in pants.base?
    pants_dev_mode = os.environ.get('PANTS_DEV')

    if pants_dev_mode:
      checker_id = self.checker_target.transitive_invalidation_hash()
    else:
      checker_id = hash_all([self._CHECKER_REQ])

    pex_path = os.path.join(self.workdir, 'checker', checker_id, str(interpreter.identity))

    if not os.path.exists(pex_path):
      with self.context.new_workunit(name='build-checker'):
        with safe_concurrent_creation(pex_path) as chroot:
          builder = PEXBuilder(path=chroot, interpreter=interpreter)
          # Constraining is required to guard against the case where the user
          # has a pexrc file set.
          builder.add_interpreter_constraint(str(interpreter.identity.requirement))

          if pants_dev_mode:
            pex_build_util.dump_sources(builder, tgt=self.checker_target, log=self.context.log)
            req_libs = [tgt for tgt in self.checker_target.closure()
                        if isinstance(tgt, PythonRequirementLibrary)]
            pex_build_util.dump_requirement_libs(builder,
                                                 interpreter=interpreter,
                                                 req_libs=req_libs,
                                                 log=self.context.log)
          else:
            pex_build_util.dump_requirements(builder,
                                             interpreter=interpreter,
                                             reqs=[PythonRequirement(self._CHECKER_REQ)],
                                             log=self.context.log)
          builder.set_entry_point(self._CHECKER_ENTRYPOINT)
          builder.freeze()

    return PEX(pex_path, interpreter=interpreter)

  def checkstyle(self, interpreter, sources):
    """Iterate over sources and run checker on each file.

    Files can be suppressed with a --suppress option which takes an xml file containing
    file paths that have exceptions and the plugins they need to ignore.

    :param sources: iterable containing source file names.
    :return: (int) number of failures
    """
    checker = self.checker_pex(interpreter)

    args = [
      '--root-dir={}'.format(get_buildroot()),
      '--severity={}'.format(self.get_options().severity),
    ]
    if self.get_options().suppress:
      args.append('--suppress={}'.format(self.get_options().suppress))
    if self.get_options().strict:
      args.append('--strict')

    with temporary_file(binary_mode=False) as argfile:
      for plugin_subsystem in self.plugin_subsystems:
        options_blob = plugin_subsystem.global_instance().options_blob()
        if options_blob:
          argfile.write('--{}-options={}\n'.format(plugin_subsystem.plugin_type().name(),
                                                   options_blob))
      argfile.write('\n'.join(sources))
      argfile.close()

      args.append('@{}'.format(argfile.name))

      with self.context.new_workunit(name='pythonstyle',
                                     labels=[WorkUnitLabel.TOOL, WorkUnitLabel.LINT],
                                     cmd=' '.join(checker.cmdline(args))) as workunit:
        failure_count = checker.run(args=args,
                                    stdout=workunit.output('stdout'),
                                    stderr=workunit.output('stderr'))
        return failure_count

  def _constraints_are_py3_compatible(self, constraint_tuple):
    """
    Detect whether a tuple of compatibility constraints
    contains Python 3-compatible constraints.
    Remove this method after closing:
    https://github.com/pantsbuild/pants/issues/5764
    """
    for constraint in constraint_tuple:
      try:
        req = Requirement.parse(constraint)
      except RequirementParseError as e:
        # Coerce "naked" requirements (e.g. `>=2.7,<3` or `>=3.6`)
        self.context.log.warn(str(e))
        self.context.log.warn("Python 3 filtering for lint task encountered an invalid "
                              "interpreter constraint: {}\n Coercing to `CPython{}`."
                              .format(constraint, constraint))
        req = Requirement.parse("CPython" + constraint)
      if any(dd in req for dd in self._python_3_compatible_dists):
        return True
    return False

  def execute(self):
    """"Run Checkstyle on all found non-synthetic source files."""
    python_tgts = self.context.targets(
      lambda tgt: isinstance(tgt, (PythonTarget))
    )
    if not python_tgts:
      return
    interpreter_cache = PythonInterpreterCache(PythonSetup.global_instance(),
                                               PythonRepos.global_instance(),
                                               logger=self.context.log.debug)
    with self.invalidated(self.get_targets(self._is_checked)) as invalidation_check:
      failure_count = 0
      tgts_by_compatibility, _ = interpreter_cache.partition_targets_by_compatibility(
        [vt.target for vt in invalidation_check.invalid_vts]
      )
      for filters, targets in tgts_by_compatibility.items():
        if not self.get_options().enable_py3_lint and self._constraints_are_py3_compatible(filters):
          deprecated_conditional(
            lambda: not self.get_options().enable_py3_lint,
            '1.14.0.dev2',
            "Python 3 linting is currently disabled by default and this disabling will be removed in the "
            "future. Use the `--enable-py3-lint` lint option to enable linting for Python 3."
          )
        else:
          sources = self.calculate_sources([tgt for tgt in targets])
          if sources:
            allowed_interpreters = set(interpreter_cache.setup(filters=filters))
            if not allowed_interpreters:
              raise TaskError('No valid interpreters found for targets: {}\n(filters: {})'
                              .format(targets, filters))
            interpreter = min(allowed_interpreters)
            failure_count += self.checkstyle(interpreter, sources)
      if failure_count > 0 and self.get_options().fail:
        raise TaskError('{} Python Style issues found. You may try `./pants fmt <targets>`'
                        .format(failure_count))
      return failure_count

  def calculate_sources(self, targets):
    """Generate a set of source files from the given targets."""
    sources = set()
    for target in targets:
      sources.update(
        source for source in target.sources_relative_to_buildroot()
        if source.endswith(self._PYTHON_SOURCE_EXTENSION)
      )
    return sources
