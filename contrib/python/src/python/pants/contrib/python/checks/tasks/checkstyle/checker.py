# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import re
from collections import namedtuple

from pants.backend.python.interpreter_cache import PythonInterpreterCache
from pants.backend.python.subsystems.python_repos import PythonRepos
from pants.backend.python.subsystems.python_setup import PythonSetup
from pants.backend.python.targets.python_target import PythonTarget
from pants.base.build_environment import get_buildroot
from pants.base.exceptions import TaskError
from pants.option.custom_types import file_option
from pants.task.lint_task_mixin import LintTaskMixin
from pants.task.task import Task

from pants.contrib.python.checks.tasks.checkstyle.common import CheckSyntaxError, Nit, PythonFile
from pants.contrib.python.checks.tasks.checkstyle.file_excluder import FileExcluder
from pants.contrib.python.checks.tasks.checkstyle.register_plugins import register_plugins

from pkg_resources import Distribution, Requirement


_NOQA_LINE_SEARCH = re.compile(r'# noqa\b').search
_NOQA_FILE_SEARCH = re.compile(r'# (flake8|checkstyle): noqa$').search


class LintPlugin(namedtuple('_LintPlugin', ['name', 'subsystem'])):
  def skip(self):
    return self.subsystem.global_instance().get_options().skip

  def checker(self, python_file):
    return self.subsystem.global_instance().get_plugin(python_file)


def line_contains_noqa(line):
  return _NOQA_LINE_SEARCH(line) is not None


def noqa_file_filter(python_file):
  return any(_NOQA_FILE_SEARCH(line) is not None for line in python_file.lines)


class PythonCheckStyleTask(LintTaskMixin, Task):
  _PYTHON_SOURCE_EXTENSION = '.py'
  _plugins = []
  _subsystems = (PythonSetup, PythonRepos)

  def __init__(self, *args, **kwargs):
    super(PythonCheckStyleTask, self).__init__(*args, **kwargs)
    self._plugins = [plugin for plugin in self._plugins if not plugin.skip()]
    self.options = self.get_options()
    self.excluder = FileExcluder(self.options.suppress, self.context.log)

  @classmethod
  def subsystem_dependencies(cls):
    return super(PythonCheckStyleTask, cls).subsystem_dependencies() + cls._subsystems

  @classmethod
  def register_options(cls, register):
    super(PythonCheckStyleTask, cls).register_options(register)
    register('--severity', fingerprint=True, default='COMMENT', type=str,
             help='Only messages at this severity or higher are logged. [COMMENT WARNING ERROR].')
    register('--strict', fingerprint=True, type=bool,
             help='If enabled, have non-zero exit status for any nit at WARNING or higher.')
    register('--suppress', fingerprint=True, type=file_option, default=None,
             help='Takes a text file where specific rules on specific files will be skipped.')
    register('--fail', fingerprint=True, default=True, type=bool,
             help='Prevent test failure but still produce output for problems.')

  @classmethod
  def supports_passthru_args(cls):
    return True

  def _is_checked(self, target):
    return (not target.is_synthetic and isinstance(target, PythonTarget) and
            target.has_sources(self._PYTHON_SOURCE_EXTENSION))

  @classmethod
  def clear_plugins(cls):
    """Clear all current plugins registered."""
    cls._plugins = []

  @classmethod
  def register_plugin(cls, name, subsystem):
    """Register plugin to be run as part of Python Style checks.

    :param string name: Name of the plugin.
    :param PluginSubsystemBase subsystem: Plugin subsystem subclass.
    """
    plugin = LintPlugin(name=name, subsystem=subsystem)
    cls._plugins.append(plugin)
    cls._subsystems += (plugin.subsystem, )

  def get_nits(self, filename):
    """Iterate over the instances style checker and yield Nits.

    :param filename: str pointing to a file within the buildroot.
    """
    try:
      python_file = PythonFile.parse(filename, root=get_buildroot())
    except CheckSyntaxError as e:
      yield e.as_nit()
      return

    if noqa_file_filter(python_file):
      return

    if self.options.suppress:
      # Filter out any suppressed plugins
      check_plugins = [plugin for plugin in self._plugins
                       if self.excluder.should_include(filename, plugin.name)]
    else:
      check_plugins = self._plugins

    for plugin in check_plugins:

      for i, nit in enumerate(plugin.checker(python_file)):
        if i == 0:
          # NB: Add debug log header for nits from each plugin, but only if there are nits from it.
          self.context.log.debug('Nits from plugin {} for {}'.format(plugin.name, filename))

        if not nit.has_lines_to_display:
          yield nit
          continue

        if all(not line_contains_noqa(line) for line in nit.lines):
          yield nit

  def check_file(self, filename):
    """Process python file looking for indications of problems.

    :param filename: (str) Python source filename
    :return: (int) number of failures
    """
    # If the user specifies an invalid severity use comment.
    log_threshold = Nit.SEVERITY.get(self.options.severity, Nit.COMMENT)

    failure_count = 0
    fail_threshold = Nit.WARNING if self.options.strict else Nit.ERROR

    for i, nit in enumerate(self.get_nits(filename)):
      if i == 0:
        print()  # Add an extra newline to clean up the output only if we have nits.
      if nit.severity >= log_threshold:
        print('{nit}\n'.format(nit=nit))
      if nit.severity >= fail_threshold:
        failure_count += 1
    return failure_count

  def checkstyle(self, sources):
    """Iterate over sources and run checker on each file.

    Files can be suppressed with a --suppress option which takes an xml file containing
    file paths that have exceptions and the plugins they need to ignore.

    :param sources: iterable containing source file names.
    :return: (int) number of failures
    """
    failure_count = 0
    for filename in sources:
      failure_count += self.check_file(filename)

    if failure_count > 0 and self.options.fail:
      raise TaskError(
        '{} Python Style issues found. You may try `./pants fmt <targets>`'.format(failure_count))
    return failure_count

  @classmethod
  def _context_has_python_3_compatible_targets(cls, tgts_by_compatibility):
    """
    Detect whether a dict of targets keyed by compatibility constraints
    contains Python 3-compatible targets. Remove this method after closing:
    https://github.com/pantsbuild/pants/issues/5764
    """
    DISALLOWED_DISTS = [
      Distribution(project_name="CPython", version="3.0"),
      Distribution(project_name="CPython", version="3.1"),
      Distribution(project_name="CPython", version="3.2"),
      Distribution(project_name="CPython", version="3.3"),
      Distribution(project_name="CPython", version="3.4"),
      Distribution(project_name="CPython", version="3.5"),
      Distribution(project_name="CPython", version="3.6"),
      Distribution(project_name="CPython", version="3.7"),
      Distribution(project_name="CPython", version="3.8"),
      Distribution(project_name="CPython", version="3.9")
    ]

    # Check for Python 3-only constraints.
    contains_py_3_compatible_tgts = False
    for constraint_tuple in tgts_by_compatibility.copy():
      for constraint in constraint_tuple:
        req = Requirement.parse(constraint)
        if any(dd in req for dd in DISALLOWED_DISTS):
          contains_py_3_compatible_tgts = True
          del tgts_by_compatibility[constraint_tuple]
          break
    return contains_py_3_compatible_tgts

  def execute(self):
    """Run Checkstyle on all found non-synthetic source files."""

    # If we are linting for Python 3, skip lint altogether.
    # Long-term Python 3 linting solution tracked by:
    # https://github.com/pantsbuild/pants/issues/5764
    python_tgts = self.context.targets(
      lambda tgt: isinstance(tgt, (PythonTarget))
    )
    if not python_tgts:
      return
    interpreter_cache = PythonInterpreterCache(PythonSetup.global_instance(),
                                               PythonRepos.global_instance(),
                                               logger=self.context.log.debug)
    tgts_by_compatibility, _ = interpreter_cache.partition_targets_by_compatibility(python_tgts)

    if self. _context_has_python_3_compatible_targets(tgts_by_compatibility):
      self.context.log.info('Linting is currently disabled for Python 3 targets.\n'
                            'They have been filtered out of the target set for this lint run.\n'
                            'See https://github.com/pantsbuild/pants/issues/5764 for '
                            'long-term solution tracking.\n\n')

    # The target dict is empty, meaning the current context has only Python 3 targets.
    if not tgts_by_compatibility:
      return

    # Flatten the targets list into a single list of targets.
    safe_targets = []
    for tgts in tgts_by_compatibility.values():
      safe_targets.extend(tgts)

    ### End of Python 3 Lint skipping workaround. ###
    ### Delete code from here up after closing https://github.com/pantsbuild/pants/issues/5764. ###

    with self.invalidated(self.get_targets(self._is_checked)) as invalidation_check:
      sources = self.calculate_sources(
        # TODO(clivingston): remove if guard after closing pants/issues/5764.
        [vt.target for vt in invalidation_check.invalid_vts if vt.target in safe_targets]
      )
      if sources:
        return self.checkstyle(sources)

  def calculate_sources(self, targets):
    """Generate a set of source files from the given targets."""
    sources = set()
    for target in targets:
      sources.update(
        source for source in target.sources_relative_to_buildroot()
        if source.endswith(self._PYTHON_SOURCE_EXTENSION)
      )
    return sources


register_plugins(PythonCheckStyleTask)
