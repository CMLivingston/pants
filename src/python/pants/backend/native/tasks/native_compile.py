# coding=utf-8
# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os
from abc import abstractmethod
from collections import defaultdict

from pants.backend.native.config.environment import Executable
from pants.backend.native.targets.external_native_library import ExternalNativeLibrary
from pants.backend.native.targets.native_library import NativeLibrary
from pants.backend.native.tasks.native_external_library_fetch import NativeExternalLibraryFiles
from pants.backend.native.tasks.native_task import NativeTask
from pants.base.build_environment import get_buildroot
from pants.base.exceptions import TaskError
from pants.base.workunit import WorkUnit, WorkUnitLabel
from pants.util.memo import memoized_method, memoized_property
from pants.util.meta import AbstractClass, classproperty
from pants.util.objects import SubclassesOf, datatype
from pants.util.process_handler import subprocess


class NativeCompileRequest(datatype([
    ('compiler', SubclassesOf(Executable)),
    # TODO: add type checking for Collection.of(<type>)!
    'include_dirs',
    'sources',
    'compiler_options',
    'output_dir',
])): pass


# TODO(#5950): perform all process execution in the v2 engine!
class ObjectFiles(datatype(['root_dir', 'filenames'])):

  def file_paths(self):
    return [os.path.join(self.root_dir, fname) for fname in self.filenames]


class NativeCompile(NativeTask, AbstractClass):
  # `NativeCompile` will use the `source_target_constraint` to determine what targets have "sources"
  # to compile, and the `dependent_target_constraint` to determine which dependent targets to
  # operate on for `strict_deps` calculation.
  # NB: `source_target_constraint` must be overridden.
  source_target_constraint = None
  dependent_target_constraint = SubclassesOf(ExternalNativeLibrary, NativeLibrary)

  # `NativeCompile` will use `workunit_label` as the name of the workunit when executing the
  # compiler process. `workunit_label` must be set to a string.
  @classproperty
  def workunit_label(cls):
    raise NotImplementedError('subclasses of NativeCompile must override workunit_label!')

  @classmethod
  def product_types(cls):
    return [ObjectFiles]

  @classmethod
  def prepare(cls, options, round_manager):
    super(NativeCompile, cls).prepare(options, round_manager)
    round_manager.optional_data(NativeExternalLibraryFiles)

  @property
  def cache_target_dirs(self):
    return True

  @classmethod
  def implementation_version(cls):
    return super(NativeCompile, cls).implementation_version() + [('NativeCompile', 1)]

  class NativeCompileError(TaskError):
    """Raised for errors in this class's logic.

    Subclasses are advised to create their own exception class.
    """

  def execute(self):
    object_files_product = self.context.products.get(ObjectFiles)
    external_libs_product = self.context.products.get_data(NativeExternalLibraryFiles)
    source_targets = self.context.targets(self.source_target_constraint.satisfied_by)

    with self.invalidated(source_targets, invalidate_dependents=True) as invalidation_check:
      for vt in invalidation_check.all_vts:
        deps = self.native_deps(vt.target)
        if not vt.valid:
          compile_request = self._make_compile_request(vt, deps, external_libs_product)
          self.context.log.debug("compile_request: {}".format(compile_request))
          self._compile(compile_request)

        object_files = self.collect_cached_objects(vt)
        self._add_product_at_target_base(object_files_product, vt.target, object_files)

  # This may be calculated many times for a target, so we memoize it.
  @memoized_method
  def _include_dirs_for_target(self, target):
    return os.path.join(get_buildroot(), target.address.spec_path)

  class NativeSourcesByType(datatype(['rel_root', 'headers', 'sources'])): pass

  def get_sources_headers_for_target(self, target):
    """Return a list of file arguments to provide to the compiler.

    NB: result list will contain both header and source files!

    :raises: :class:`NativeCompile.NativeCompileError` if there is an error processing the sources.
    """
    # Get source paths relative to the target base so the exception message with the target and
    # paths makes sense.
    target_relative_sources = target.sources_relative_to_target_base()
    rel_root = target_relative_sources.rel_root

    # Unique file names are required because we just dump object files into a single directory, and
    # the compiler will silently just produce a single object file if provided non-unique filenames.
    # TODO: add some shading to file names so we can remove this check.
    # NB: It shouldn't matter if header files have the same name, but this will raise an error in
    # that case as well. We won't need to do any shading of header file names.
    seen_filenames = defaultdict(list)
    for src in target_relative_sources:
      seen_filenames[os.path.basename(src)].append(src)
    duplicate_filename_err_msgs = []
    for fname, source_paths in seen_filenames.items():
      if len(source_paths) > 1:
        duplicate_filename_err_msgs.append("filename: {}, paths: {}".format(fname, source_paths))
    if duplicate_filename_err_msgs:
      raise self.NativeCompileError(
        "Error in target '{}': source files must have a unique filename within a '{}' target. "
        "Conflicting filenames:\n{}"
        .format(target.address.spec, target.alias(), '\n'.join(duplicate_filename_err_msgs)))

    return [os.path.join(get_buildroot(), rel_root, src) for src in target_relative_sources]

  @abstractmethod
  def get_compile_settings(self):
    """Return an instance of NativeBuildStepSettings.

    NB: Subclasses will be queried for the compile settings once and the result cached.
    """

  @memoized_property
  def _compile_settings(self):
    return self.get_compile_settings()

  @abstractmethod
  def get_compiler(self):
    """An instance of `Executable` which can be invoked to compile files.

    NB: Subclasses will be queried for the compiler instance once and the result cached.

    :return: :class:`pants.backend.native.config.environment.Executable`
    """

  @memoized_property
  def _compiler(self):
    return self.get_compiler()

  def _get_third_party_include_dirs(self, external_libs_product, dependencies):
    if not external_libs_product:
      return []

    return [nelf.include_dir
            for nelf in external_libs_product.get_for_targets(dependencies)
            if nelf.include_dir]

  def _make_compile_request(self, versioned_target, dependencies, external_libs_product):
    target = versioned_target.target

    include_dirs = [self._include_dirs_for_target(dep_tgt) for dep_tgt in dependencies]
    include_dirs.extend(self._get_third_party_include_dirs(external_libs_product, dependencies))
    sources_and_headers = self.get_sources_headers_for_target(target)
    compiler_option_sets = (self._compile_settings.native_build_step_settings
                                .get_compiler_option_sets_for_target(target))

    return NativeCompileRequest(
      compiler=self._compiler,
      include_dirs=include_dirs,
      sources=sources_and_headers,
      compiler_options=(self._compile_settings
                            .native_build_step_settings
                            .get_merged_args_for_compiler_option_sets(compiler_option_sets)),
      output_dir=versioned_target.results_dir)

  def _make_compile_argv(self, compile_request):
    """Return a list of arguments to use to compile sources. Subclasses can override and append."""
    compiler = compile_request.compiler
    compiler_options = compile_request.compiler_options
    # We are going to execute in the target output, so get absolute paths for everything.
    buildroot = get_buildroot()
    argv = (
      [compiler.exe_filename] +
      compiler.extra_args +
      # TODO: If we need to produce static libs, don't add -fPIC! (could use Variants -- see #5788).
      ['-c', '-fPIC'] +
      compiler_options +
      [
        '-I{}'.format(os.path.join(buildroot, inc_dir))
        for inc_dir in compile_request.include_dirs
      ] +
      [os.path.join(buildroot, src) for src in compile_request.sources])

    self.context.log.debug("compile argv: {}".format(argv))

    return argv

  def _compile(self, compile_request):
    """Perform the process of compilation, writing object files to the request's 'output_dir'.

    NB: This method must arrange the output files so that `collect_cached_objects()` can collect all
    of the results (or vice versa)!
    """
    sources = compile_request.sources

    if len(sources) == 0:
      # TODO: do we need this log message? Should we still have it for intentionally header-only
      # libraries (that might be a confusing message to see)?
      self.context.log.debug("no sources in request {}, skipping".format(compile_request))
      return

    compiler = compile_request.compiler
    output_dir = compile_request.output_dir

    argv = self._make_compile_argv(compile_request)
    env = compiler.as_invocation_environment_dict

    with self.context.new_workunit(
        name=self.workunit_label, labels=[WorkUnitLabel.COMPILER]) as workunit:
      try:
        process = subprocess.Popen(
          argv,
          cwd=output_dir,
          stdout=workunit.output('stdout'),
          stderr=workunit.output('stderr'),
          env=env)
      except OSError as e:
        workunit.set_outcome(WorkUnit.FAILURE)
        raise self.NativeCompileError(
          "Error invoking '{exe}' with command {cmd} and environment {env} for request {req}: {err}"
          .format(exe=compiler.exe_filename, cmd=argv, env=env, req=compile_request, err=e))

      rc = process.wait()
      if rc != 0:
        workunit.set_outcome(WorkUnit.FAILURE)
        raise self.NativeCompileError(
          "Error in '{section_name}' with command {cmd} and environment {env} for request {req}. "
          "Exit code was: {rc}."
          .format(section_name=self.workunit_label, cmd=argv, env=env, req=compile_request, rc=rc))

  def collect_cached_objects(self, versioned_target):
    """Scan `versioned_target`'s results directory and return the output files from that directory.

    :return: :class:`ObjectFiles`
    """
    return ObjectFiles(versioned_target.results_dir, os.listdir(versioned_target.results_dir))
