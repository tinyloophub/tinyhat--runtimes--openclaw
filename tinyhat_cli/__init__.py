"""Tinyhat on-box CLI + extracted runtime units.

This package is the structural home for runtime capabilities extracted
out of ``supervisor.py`` (v0.12.0 M1): a thin static registry of typed
command handlers, a single OpenClaw adapter boundary, and the read
units behind the root-only ``tinyhat`` diagnose CLI (``status``,
``health``, ``manifest show|drift``, ``whoami``).

Layout:

- ``tinyhat_cli.entrypoint`` — the root-only argv entrypoint installed
  as ``/usr/local/bin/tinyhat`` by ``bootstrap.sh``.
- ``tinyhat_cli.registry`` — the static command registry + typed
  handler contract.
- ``tinyhat_cli.adapters.openclaw`` — the only module in this package
  allowed to talk to the ``openclaw`` binary.
- ``tinyhat_cli.units.*`` — extracted units. Moved functions keep
  delegating re-exports in ``supervisor.py`` and resolve their
  cross-module dependencies through :mod:`tinyhat_cli._facade`, so the
  characterization test net (``tests/test_supervisor.py``) passes
  unmodified and its ``patch.object(supervisor, ...)`` targets keep
  applying to extracted code paths.

The authoritative list of what moved from where is
``tinyhat_cli/extraction_map.json`` (enforced by
``tests/test_extraction_guards.py``).
"""
