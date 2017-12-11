# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
import sys
from contextlib import contextmanager

import mock
from pex.package import EggPackage, Package, SourcePackage
from pex.resolver import Unsatisfiable, resolve

from pants.backend.python.interpreter_cache import PythonInterpreter, PythonInterpreterCache
from pants.backend.python.subsystems.python_setup import PythonSetup
from pants.python.python_repos import PythonRepos
from pants.util.contextutil import temporary_dir
from pants_test.base_test import BaseTest
from pants_test.testutils.pexrc_util import (ensure_python_interpreter,
                                             setup_pexrc_with_pex_python_path)


class TestInterpreterCache(BaseTest):
  @staticmethod
  def _make_bad_requirement(requirement):
    """Turns a requirement that passes into one we know will fail.

    E.g. 'CPython==2.7.5' becomes 'CPython==99.7.5'
    """
    return str(requirement).replace('==2.', '==99.')

  def setUp(self):
    super(TestInterpreterCache, self).setUp()
    self._interpreter = PythonInterpreter.get()

  def get_project_root(self):
    return os.path.abspath(os.path.join(os.path.dirname(__file__),"..", "..", "..", "..", "..", "..", ".."))

  @contextmanager
  def _setup_test(self, constraints=None, mock_setup_paths_with_interpreters=False):
    mock_setup = mock.MagicMock().return_value
    type(mock_setup).interpreter_constraints = mock.PropertyMock(return_value=constraints)

    with temporary_dir() as path:
      mock_setup.interpreter_cache_dir = path
      cache = PythonInterpreterCache(mock_setup, mock.MagicMock())
      cache._setup_cached = mock.Mock(return_value=[self._interpreter])
      if mock_setup_paths_with_interpreters:
        cache._setup_paths = mock.Mock(return_value=[PythonInterpreter.from_binary(ensure_python_interpreter('2.7.10', self.get_project_root())),
                                                     PythonInterpreter.from_binary(ensure_python_interpreter('3.6.3', self.get_project_root())])
      else:
        cache._setup_paths = mock.Mock(return_value=[])
      yield cache, path

  def _do_test(self, constraints, filters, expected):
    with self._setup_test(constraints) as (cache, _):
      self.assertEqual(cache.setup(filters=filters), expected)

  def test_cache_setup_with_no_filters_uses_repo_default_excluded(self):
    self._do_test([self._make_bad_requirement(self._interpreter.identity.requirement)], [], [])

  def test_cache_setup_with_no_filters_uses_repo_default(self):
    self._do_test((b'',), [], [self._interpreter])

  def test_cache_setup_with_filter_overrides_repo_default(self):
    self._do_test([self._make_bad_requirement(self._interpreter.identity.requirement)],
                  (str(self._interpreter.identity.requirement), ),
                  [self._interpreter])

  def test_setup_using_eggs(self):
    def link_egg(repo_root, requirement):
      existing_dist_location = self._interpreter.get_location(requirement)
      if existing_dist_location is not None:
        existing_dist = Package.from_href(existing_dist_location)
        requirement = '{}=={}'.format(existing_dist.name, existing_dist.raw_version)

      distributions = resolve([requirement],
                              interpreter=self._interpreter,
                              precedence=(EggPackage, SourcePackage))
      self.assertEqual(1, len(distributions))
      dist_location = distributions[0].location

      self.assertRegexpMatches(dist_location, r'\.egg$')
      os.symlink(dist_location, os.path.join(repo_root, os.path.basename(dist_location)))

      return Package.from_href(dist_location).raw_version

    with temporary_dir() as root:
      egg_dir = os.path.join(root, 'eggs')
      os.makedirs(egg_dir)
      setuptools_version = link_egg(egg_dir, 'setuptools')
      wheel_version = link_egg(egg_dir, 'wheel')

      interpreter_requirement = self._interpreter.identity.requirement

      self.context(for_subsystems=[PythonSetup, PythonRepos], options={
        PythonSetup.options_scope: {
          'interpreter_cache_dir': None,
          'pants_workdir': os.path.join(root, 'workdir'),
          'constraints': [interpreter_requirement],
          'setuptools_version': setuptools_version,
          'wheel_version': wheel_version,
        },
        PythonRepos.options_scope: {
          'indexes': [],
          'repos': [egg_dir],
        }
      })
      cache = PythonInterpreterCache(PythonSetup.global_instance(), PythonRepos.global_instance())

      interpereters = cache.setup(paths=[os.path.dirname(self._interpreter.binary)],
                                  filters=[str(interpreter_requirement)])
      self.assertGreater(len(interpereters), 0)

      def assert_egg_extra(interpreter, name, version):
        location = interpreter.get_location('{}=={}'.format(name, version))
        self.assertIsNotNone(location)
        self.assertIsInstance(Package.from_href(location), EggPackage)

      for interpreter in interpereters:
        assert_egg_extra(interpreter, 'setuptools', setuptools_version)
        assert_egg_extra(interpreter, 'wheel', wheel_version)

  def test_setup_resolve_failure_cleanup(self):
    """Simulates a resolution failure during interpreter setup to avoid partial interpreter caching.

    See https://github.com/pantsbuild/pants/issues/2038 for more info.
    """
    with mock.patch.object(PythonInterpreterCache, '_resolve') as mock_resolve, \
         self._setup_test() as (cache, cache_path):
      mock_resolve.side_effect = Unsatisfiable('nope')

      with self.assertRaises(Unsatisfiable):
        cache._setup_interpreter(self._interpreter, os.path.join(cache_path, 'CPython-2.7.11'))

      # Before the bugfix, the above call would leave behind paths in the tmpdir that looked like:
      #
      #     /tmp/tmpUrCSzk/CPython-2.7.11.tmp.a167fc50834a4f00aa280780c3e1ba21
      #
      self.assertFalse('.tmp.' in ' '.join(os.listdir(cache_path)),
                       'interpreter cache path contains tmp dirs!')

  def test_pex_python_paths(self):
    """Test pex python path helper method of PythonInterpreterCache."""
    with self._setup_test() as (cache, cache_path):
      py27 = ensure_python_interpreter('2.7.10', self.get_project_root())
      py36 = ensure_python_interpreter('3.6.3', self.get_project_root())
      with setup_pexrc_with_pex_python_path(os.path.dirname(sys.argv[0]), [py27, py36]):
        pex_python_paths = cache.pex_python_paths()
        self.assertEqual(pex_python_paths, [py27, py36])

  def test_interpereter_cache_setup_using_pex_python_paths(self): 
    """Test cache setup using interpreters from a mocked PEX_PYTHON_PATH."""
    py27 = ensure_python_interpreter('2.7.10', self.get_project_root())
    py36 = ensure_python_interpreter('3.6.3', self.get_project_root())
    print(os.path.exists(os.path.join(self.get_project_root(), '.pyenv_test')))
    if os.path.exists(os.path.join(self.get_project_root(), '.pyenv_test')):
      print(os.listdir(os.path.join(self.get_project_root(), '.pyenv_test', 'versions', '2.7.10', 'bin')))
    # Target python 2 for interpreter cache.
    with self._setup_test(constraints=['<3,<2.7.14'], mock_setup_paths_with_interpreters=True) as (cache, cache_path):
      interpereters = cache.setup()
      self.assertEqual(len(interpereters), 1)
      self.assertEqual(interpereters[0].binary, py27)
    # Target python 3 for interpreter cache.
    with self._setup_test(constraints=['>3,<=3.6.3'], mock_setup_paths_with_interpreters=True) as (cache, cache_path):
      interpereters = cache.setup()
      self.assertEqual(len(interpereters), 1)
      self.assertEqual(interpereters[0].binary, py36)
