"""Adapter boundary modules.

``openclaw.py`` is the ONLY module in the ``tinyhat_cli`` package
allowed to invoke the ``openclaw`` binary or build its CLI
environment. ``tests/test_extraction_guards.py`` enforces the boundary
with a source scan (plus a deliberate red test proving the scan can
fail).
"""
