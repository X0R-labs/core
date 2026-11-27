"""The guard now lives in the ``x0r-guard`` package (``import x0r_guard``).

This keeps old imports working: ``x0r_core.guard`` and ``x0r_core.guard.engine``,
``.identity`` and ``.sdk`` are the very same modules as in ``x0r_guard``.
"""
import sys

from x0r_guard import *  # noqa: F403
from x0r_guard import __all__ as __all__
from x0r_guard import engine, identity, sdk

for _module in (engine, identity, sdk):
    sys.modules[f"{__name__}.{_module.__name__.rsplit('.', 1)[1]}"] = _module
del _module
