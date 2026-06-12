"""Extraction guards for the v0.12.0 M1 read-unit extraction.

These tests ARE the CI guards the milestone promises:

- the audited extraction map is enforced (every mapped name importable
  at its destination, the supervisor attribute IS the destination
  object — delegation, not duplication — and no duplicate live
  implementation remains in supervisor.py);
- the whole-file checkpoint (supervisor.py never grows past its
  pre-extraction baseline);
- new-file absolute budgets (entrypoint <= 300 lines, registry <= 200);
- the single-OpenClaw-adapter boundary for the new package, with a
  deliberate red fixture proving the scanner can fail.

Usage:
    python -m unittest tests.test_extraction_guards -v
"""

from __future__ import annotations

import importlib
import json
import os
import re
import unittest

import supervisor

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pre-extraction supervisor.py line count at the extraction map's
# base commit. The M1 whole-file checkpoint: supervisor.py must never
# end the milestone above this number (unrelated growth cannot ride
# the extraction).
SUPERVISOR_LINE_BASELINE = 7083

ENTRYPOINT_LINE_BUDGET = 300
REGISTRY_LINE_BUDGET = 200


def _read_repo_file(*parts: str) -> str:
    with open(os.path.join(_REPO_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load_extraction_map() -> dict:
    return json.loads(_read_repo_file("tinyhat_cli", "extraction_map.json"))


class ExtractionMapGuardTests(unittest.TestCase):
    def test_map_exists_and_is_well_formed(self) -> None:
        doc = _load_extraction_map()
        self.assertEqual(doc["schema"], "tinyhat_extraction_map_v1")
        self.assertEqual(doc["supervisor_line_count_baseline"], SUPERVISOR_LINE_BASELINE)
        self.assertGreater(len(doc["moves"]), 0)
        for move in doc["moves"]:
            self.assertIn(move["kind"], ("function", "constant"))
            start, end = move["source_range"]
            self.assertLessEqual(start, end)

    def test_every_mapped_name_is_a_delegating_reexport(self) -> None:
        """supervisor.<name> must BE the destination object (no copy)."""
        doc = _load_extraction_map()
        for move in doc["moves"]:
            name = move["name"]
            module = importlib.import_module(move["destination"])
            self.assertTrue(
                hasattr(module, name),
                f"{move['destination']} is missing moved name {name}",
            )
            self.assertTrue(
                hasattr(supervisor, name),
                f"supervisor lost the re-export for {name}",
            )
            self.assertIs(
                getattr(supervisor, name),
                getattr(module, name),
                f"supervisor.{name} is not the same object as "
                f"{move['destination']}.{name} — duplicate implementation?",
            )

    def test_no_duplicate_live_implementation_in_supervisor(self) -> None:
        """A moved function may not keep a second `def` in supervisor.py."""
        source = _read_repo_file("supervisor.py")
        doc = _load_extraction_map()
        for move in doc["moves"]:
            if move["kind"] != "function":
                continue
            pattern = re.compile(rf"^def {re.escape(move['name'])}\(", re.MULTILINE)
            self.assertIsNone(
                pattern.search(source),
                f"supervisor.py still defines {move['name']} — the extraction "
                "left a duplicate live implementation",
            )

    def test_no_duplicate_constant_assignment_in_supervisor(self) -> None:
        source = _read_repo_file("supervisor.py")
        doc = _load_extraction_map()
        for move in doc["moves"]:
            if move["kind"] != "constant":
                continue
            pattern = re.compile(
                rf"^{re.escape(move['name'])}(?::[^=]+)?\s*=\s*(?!.*\bimport\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(source),
                f"supervisor.py still assigns {move['name']} — the extraction "
                "left a duplicate constant",
            )


class LineBudgetGuardTests(unittest.TestCase):
    def _line_count(self, *parts: str) -> int:
        return len(_read_repo_file(*parts).splitlines())

    def test_supervisor_whole_file_checkpoint(self) -> None:
        count = self._line_count("supervisor.py")
        self.assertLessEqual(
            count,
            SUPERVISOR_LINE_BASELINE,
            f"supervisor.py is {count} lines — above the M1 whole-file "
            f"checkpoint of {SUPERVISOR_LINE_BASELINE}. Unrelated growth "
            "may not ride the extraction.",
        )

    def test_entrypoint_budget(self) -> None:
        count = self._line_count("tinyhat_cli", "entrypoint.py")
        self.assertLessEqual(
            count, ENTRYPOINT_LINE_BUDGET, f"entrypoint.py is {count} lines"
        )

    def test_registry_budget(self) -> None:
        count = self._line_count("tinyhat_cli", "registry.py")
        self.assertLessEqual(
            count, REGISTRY_LINE_BUDGET, f"registry.py is {count} lines"
        )


# ── single-adapter boundary ──────────────────────────────────────────

# Reaching OpenClaw means one of two things: spawning the ``openclaw``
# binary (a subprocess call whose argv head is "openclaw", or a shell
# string starting with it), or building the binary's CLI environment
# (``_openclaw_cli_env``). Mentioning the word in a log message or a
# payload key (``payload["openclaw"]``) is not access — so the scan is
# AST-shaped, not a substring grep.
_ADAPTER_RELPATH = os.path.join("tinyhat_cli", "adapters", "openclaw.py")
_SUBPROCESS_CALL_NAMES = frozenset(
    {"run", "Popen", "check_output", "check_call", "call"}
)


def _scan_source_for_openclaw_literal(source: str) -> bool:
    """True when the source reaches for OpenClaw (boundary violation)."""
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_openclaw_cli_env":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "_openclaw_cli_env":
            return True
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if func_name not in _SUBPROCESS_CALL_NAMES or not node.args:
            continue
        first = node.args[0]
        head: str | None = None
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            element = first.elts[0]
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                head = element.value
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            head = first.value.split(" ", 1)[0] if first.value else None
        if head is not None and (head == "openclaw" or head.startswith("openclaw")):
            return True
    return False


def _package_python_files() -> list[str]:
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(os.path.join(_REPO_ROOT, "tinyhat_cli")):
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(os.path.join(dirpath, filename))
    return sorted(files)


class AdapterBoundaryGuardTests(unittest.TestCase):
    def test_no_openclaw_access_outside_adapter(self) -> None:
        violations: list[str] = []
        for path in _package_python_files():
            rel = os.path.relpath(path, _REPO_ROOT)
            if rel == _ADAPTER_RELPATH:
                continue
            with open(path, encoding="utf-8") as fh:
                if _scan_source_for_openclaw_literal(fh.read()):
                    violations.append(rel)
        self.assertEqual(
            violations,
            [],
            "OpenClaw access outside the adapter boundary: " + ", ".join(violations),
        )

    def test_adapter_itself_reaches_openclaw(self) -> None:
        """Sanity: the adapter is where the literals actually live."""
        source = _read_repo_file("tinyhat_cli", "adapters", "openclaw.py")
        self.assertTrue(_scan_source_for_openclaw_literal(source))

    def test_scanner_flags_a_violation(self) -> None:
        """Deliberate red fixture: prove the boundary scan CAN fail."""
        violating_source = (
            "import subprocess\n"
            'result = subprocess.run(["openclaw", "plugins", "list"])\n'
        )
        self.assertTrue(
            _scan_source_for_openclaw_literal(violating_source),
            "the adapter-boundary scanner failed to flag a synthetic "
            "violation — the guard is vacuous",
        )


class RegistryContractGuardTests(unittest.TestCase):
    def test_registry_is_static_root_only_and_categorized(self) -> None:
        from tinyhat_cli.registry import ALLOWED_UNIT_CATEGORIES, build_registry

        registry = build_registry()
        self.assertEqual(
            sorted(registry),
            [
                "gateway restart",
                "health",
                "manifest drift",
                "manifest show",
                "status",
                "whoami",
            ],
        )
        for spec in registry.values():
            self.assertEqual(spec.privilege, "root")
            self.assertIn(spec.category, ALLOWED_UNIT_CATEGORIES)
            if spec.command_class == "diagnose":
                self.assertFalse(spec.side_effect)
            else:
                self.assertEqual(spec.command_class, "operate")
                self.assertTrue(spec.side_effect)
                self.assertIsInstance(spec.timeout_seconds, int)

    def test_gateway_restart_is_the_only_operate_command(self) -> None:
        """The v0.12 release-blocking operate bar is exactly one command."""
        from tinyhat_cli.registry import build_registry

        operate = [
            name
            for name, spec in build_registry().items()
            if spec.command_class == "operate"
        ]
        self.assertEqual(operate, ["gateway restart"])

    def test_gateway_restart_deadline_covers_its_phases(self) -> None:
        """Declared deadline ≥ readiness bound + the stop/start allowance."""
        from tinyhat_cli.registry import build_registry

        spec = build_registry()["gateway restart"]
        readiness_bound = supervisor.OPENCLAW_GATEWAY_START_TIMEOUT_SECONDS
        stop_start_allowance = supervisor.SYSTEMCTL_TIMEOUT_SECONDS
        self.assertGreaterEqual(
            spec.timeout_seconds,
            readiness_bound + stop_start_allowance,
            "a ratified gateway-restart deadline can never undercut the "
            "readiness probe plus the synchronous systemctl allowance",
        )

    def test_registry_rejects_product_category(self) -> None:
        from tinyhat_cli.registry import CommandSpec

        with self.assertRaises(ValueError):
            CommandSpec(
                name="send-message",
                command_class="diagnose",
                category="product",
                privilege="root",
                side_effect=False,
                summary="never",
                handler=lambda ctx: {},
                render=lambda data: [],
            )


if __name__ == "__main__":
    unittest.main()
