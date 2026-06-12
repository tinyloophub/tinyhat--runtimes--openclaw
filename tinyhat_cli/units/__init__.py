"""Extracted runtime units (v0.12.0 M1).

Each module here is one unit from the extraction map. Moved functions
keep their exact behaviour; cross-module dependencies resolve through
:func:`tinyhat_cli._facade.supervisor_module` so existing supervisor
patches keep working. New units (``snapshot``, ``status``, ``health``,
``whoami``) carry their own tests in ``tests/test_tinyhat_cli.py``.
"""
