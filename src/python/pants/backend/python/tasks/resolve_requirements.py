# coding=utf-8
# Copyright 2016 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

<<<<<<< 21acbf1ada8ce9d58eb5095e3e5c573b0626cf04:src/python/pants/backend/python/tasks/resolve_requirements.py
from pants.backend.python.tasks.pex_build_util import has_python_requirements
from pants.backend.python.tasks.resolve_requirements_task_base import ResolveRequirementsTaskBase
=======
from pants.backend.python.tasks2.pex_build_util import has_python_requirements, has_python_and_c_sources
from pants.backend.python.tasks2.resolve_requirements_task_base import ResolveRequirementsTaskBase
>>>>>>> First stab at run task:src/python/pants/backend/python/tasks2/resolve_requirements.py


class ResolveRequirements(ResolveRequirementsTaskBase):
  """Resolve external Python requirements."""
  REQUIREMENTS_PEX = 'python_requirements_pex'

  @classmethod
  def product_types(cls):
    return [cls.REQUIREMENTS_PEX]

  def execute(self):
    req_libs = self.context.targets(has_python_requirements)
    if req_libs:
      pex = self.resolve_requirements(req_libs)
      self.context.products.register_data(self.REQUIREMENTS_PEX, pex)