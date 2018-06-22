# coding=utf-8
# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
from distutils.dir_util import copy_tree

from pants.backend.native.subsystems.conan_prep import ConanPrep
from pants.backend.native.targets.third_party_native_library import ThirdPartyNativeLibrary
from pants.base.exceptions import TaskError
from pants.task.task import Task
from pants.util.contextutil import environment_as
from pants.util.memo import memoized_property
from pants.util.objects import Exactly
from pants.util.osutil import get_normalized_os_name
from pants.util.process_handler import subprocess


class NativeThirdPartyPrep(Task):
  options_scope = 'native-third-party-prep'
  native_library_constraint = Exactly(ThirdPartyNativeLibrary)

  class ThirdPartyLibraryFiles(object):
    pass

  class NativeThirdPartyFetchError(TaskError):
    pass

  @staticmethod
  def translate_conan_pkg_id_to_directory_path(pkg_string):
    return pkg_string.replace('@', '/')

  @staticmethod
  def translate_pkg_string_to_pants_pkg_id(pkg_string):
    return pkg_string.replace('/', '@')

  @staticmethod
  def parse_lib_name_from_conan_pkg_id(conan_pkg_id_string):
    return conan_pkg_id_string.split('/')[0]

  @staticmethod
  def parse_lib_name_from_pants_pkg_id(pants_pkg_id_string):
    return pants_pkg_id_string.split('@')[0]

  @staticmethod
  def parse_lib_name_from_library_filename(filename):
    # TODO(cmlivingston): regex this
    return filename.split('lib')[1].split('.')[0]

  @staticmethod
  def parse_conan_stdout_for_pkg_sha(stdout, pkg_string):
    # TODO(cmlivingston): regex this
    pkg_line = stdout.split('Packages')[1]
    collected_matches = [line for line in pkg_line.split() if pkg_string in line]
    pkg_sha = collected_matches[0].split(':')[1]
    return pkg_sha

  @staticmethod
  def build_conan_cmdline(pkg_spec):
    os_name = get_normalized_os_name()
    conan_os_opt = None
    if os_name == 'linux':
      conan_os_opt = 'Linux'
    elif os_name == 'darwin':
      conan_os_opt = 'Macos'
    args = ['install', pkg_spec, '-r=pants-conan-remote']
    if conan_os_opt:
      args.extend(['-s', 'os=' + conan_os_opt])
    return args

  @classmethod
  def register_options(cls, register):
    super(NativeThirdPartyPrep, cls).register_options(register)
    register('--conan-remote', type=str, default='https://conan.bintray.com', advanced=True,
             fingerprint=True, help='The conan remote to download conan packages from.')

  @classmethod
  def subsystem_dependencies(cls):
    return super(NativeThirdPartyPrep, cls).subsystem_dependencies() + (ConanPrep,)

  @classmethod
  def product_types(cls):
    return [cls.ThirdPartyLibraryFiles]

  @property
  def cache_target_dirs(self):
    return True

  @memoized_property
  def _conan_binary(self):
    return ConanPrep.global_instance().bootstrap_conan()

  def execute(self):
    native_lib_tgts = self.context.targets(self.native_library_constraint.satisfied_by)
    with self.invalidated(native_lib_tgts,
                          invalidate_dependents=True) as invalidation_check:
      for vt in invalidation_check.all_vts:
        if vt.valid:
          self.populate_task_product(vt)
        else:
          self.fetch_packages(vt)

  def populate_task_product(self, vt):
    task_product = {}
    task_product['lib_names'] = []

    lib = os.path.join(vt.results_dir, 'lib')
    include = os.path.join(vt.results_dir, 'include')

    if os.path.exists(lib):
      task_product['lib'] = lib
      for filename in os.listdir(lib):
        lib_name = self.parse_lib_name_from_library_filename(filename)
        if lib_name:
          task_product['lib_names'].append(lib_name)

    if os.path.exists(include):
      task_product['include'] = include

    self.context.products.register_data(self.ThirdPartyLibraryFiles, task_product)

  def ensure_conan_remote_configuration(self, conan_binary):
    """
    Ensure that the conan registry.txt file is sanitized and loaded with
    a pants-specifc remote for package fetching.

    :param conan_binary: The conan client pex to use for manupulating registry.txt.
    """

    # Delete the conan-center remote from conan's registry.
    remove_conan_center_remote_cmdline = conan_binary.pex.cmdline(['remote',
                                                                   'remove',
                                                                   'conan-center'])
    try:
      stdout = subprocess.check_output(remove_conan_center_remote_cmdline.split())
      self.context.log.debug(stdout)
    except subprocess.CalledProcessError as e:
      if not "'conan-center' not found in remotes" in e.output:
        raise TaskError('Error deleting conan-center from conan registry: {}'.format(e.output))

    # Add the pants-specific conan remote.
    remote_url = self.get_options().conan_remote
    add_pants_conan_remote_cmdline = conan_binary.pex.cmdline(['remote',
                                                               'add',
                                                               'pants-conan-remote',
                                                               remote_url,
                                                               '--insert'])
    try:
      stdout = subprocess.check_output(add_pants_conan_remote_cmdline.split())
      self.context.log.debug(stdout)
    except subprocess.CalledProcessError as e:
      if not "Remote 'pants-conan-remote' already exists in remotes" in e.output:
        raise TaskError('Error adding pants-specific conan remote: {}'.format(e.output))

  def copy_package_contents_from_conan_dir(self, results_dir, pkg_spec, pkg_sha):
    """
    Copy the contents of the fetched pacakge into the results directory of the versioned
    target from the conan data directory.

    :param results_dir: The versioned-target results directory.
    :param pkg_spec: The conan package specification string.
    :param pkg_sha: The sha of the local conan package corresponding to the specification.
    """
    src = os.path.join(os.path.join(self.workdir, '.conan'),
                       'data',
                       self.translate_conan_pkg_id_to_directory_path(pkg_spec),
                       'package',
                       pkg_sha)
    src_lib = os.path.join(src, 'lib')
    src_include = os.path.join(src, 'include')
    dest_lib = os.path.join(results_dir, 'lib')
    dest_include = os.path.join(results_dir, 'include')
    if os.path.exists(src_lib):
      copy_tree(src_lib, dest_lib)
    if os.path.exists(src_include):
      copy_tree(src_include, dest_include)

  def fetch_packages(self, vt):
    """
    Invoke the conan pex to fetch conan packages specified by a
    `NativeThirdPartyLibrary` target.
    """
    task_product = {}
    task_product['lib_names'] = []

    with environment_as(CONAN_USER_HOME=self.workdir):
      for pkg_spec in vt.target.packages:

        # Prepare conan command line and ensure remote is configured properly.
        self.ensure_conan_remote_configuration(self._conan_binary)
        args = self.build_conan_cmdline(pkg_spec)
        cmdline = self._conan_binary.pex.cmdline(args)
        self.context.log.debug('Running conan.pex cmdline: {}'.format(cmdline))

        # Invoke conan to pull package from remote.
        try:
          process = subprocess.Popen(
            cmdline.split(),
            cwd=vt.results_dir,
            stdout=subprocess.PIPE
          )
        except OSError as e:
          raise self.NativeThirdPartyFetchError(
            "Error invoking conan for fetch task. Command {}:".format(cmdline), e
          )
        rc = process.wait()
        stdout = process.stdout.read()
        if rc != 0:
          raise self.NativeThirdPartyFetchError(
            "Error fetching native third party artifacts from the conan server ({}). "
            "Command: {}\n\nConan output: {}\nExit code: {}\n"
            .format(self.get_options().conan_remote, cmdline, stdout, rc))

        pkg_sha = self.parse_conan_stdout_for_pkg_sha(stdout, pkg_spec)
        self.copy_package_contents_from_conan_dir(vt.results_dir, pkg_spec, pkg_sha)

        # Populate the task product.
        dest_lib = os.path.join(vt.results_dir, 'lib')
        dest_include = os.path.join(vt.results_dir, 'include')
        if os.path.exists(dest_lib):
          task_product['lib'] = dest_lib
          for filename in os.listdir(dest_lib):
            lib_name = self.parse_lib_name_from_library_filename(filename)
            if lib_name:
              task_product['lib_names'].append(lib_name)
        else:
          self.context.log.debug('{} package did not define a lib directory.'.format(pkg_spec))
        if os.path.exists(dest_include):
          task_product['include'] = dest_include
        else:
          self.context.log.warn('{} package did not define an include directory. The compile task '
                                'may not function properly.'.format(pkg_spec))

      self.context.products.register_data(self.ThirdPartyLibraryFiles, task_product)
