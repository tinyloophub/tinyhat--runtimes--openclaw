"""Resolve the running supervisor module for extracted units.

The supervisor code runs three ways, and extracted units must bind to
the SAME module object in all of them — otherwise mutable module state
(caches, holders) and test patches would split across two copies of
``supervisor.py``:

- **Production daemon** — systemd runs ``python3 .../supervisor.py``,
  so the supervisor module IS ``__main__``. A plain ``import
  supervisor`` here would execute a second copy of the file with its
  own state. We detect that case and return ``__main__``.
- **Test suite** — tests ``import supervisor``; we resolve the already
  imported module from ``sys.modules``.
- **CLI process** — nothing has imported it yet; we import it once
  (the checkout dir is on ``sys.path`` via the ``tinyhat`` wrapper).

Extracted code calls every cross-module dependency through
:func:`supervisor_module` at call time (late binding). That is what
keeps the characterization net's ``patch.object(supervisor, ...)``
patches applying to extracted code paths: the unit resolves the name
through the supervisor module's namespace at the moment of the call,
exactly like the original module-global lookup did.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType


def supervisor_module() -> ModuleType:
    """Return the module object that owns the supervisor namespace."""
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None) if main is not None else None
    if main_file and os.path.basename(str(main_file)) == "supervisor.py":
        return main
    import supervisor

    return supervisor
