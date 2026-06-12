"""Output schemas for the ``tinyhat`` diagnose commands + a tiny validator.

The Computer image is stdlib-only, so this module carries both the
schema documents (a JSON-Schema subset) and a small validator that
understands exactly the keywords used here: ``type`` (string or list),
``const``, ``enum``, ``properties``, ``required``, ``items``.

On-box validation for the ``cli-functional-onbox`` evidence transcript::

    tinyhat status --json | python3 -m tinyhat_cli.schemas status
    tinyhat manifest drift --json | python3 -m tinyhat_cli.schemas "manifest drift"

Exit 0 = schema-valid; non-zero prints each violation with its path.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def validate(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Return a list of violations (empty = valid)."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_TYPE_CHECKS[name](instance) for name in allowed):
            errors.append(f"{path}: expected type {allowed}, got {type(instance).__name__}")
            return errors
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance:
                errors.extend(validate(instance[key], subschema, f"{path}.{key}"))
    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors


_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}

_DESIRED_STALENESS = {
    "type": "object",
    "required": ["summary"],
    "properties": {
        "creation_spec_mtime_unix": _NULLABLE_INT,
        "creation_spec_age_seconds": _NULLABLE_INT,
        "last_acked_update_mtime_unix": _NULLABLE_INT,
        "last_acked_update_age_seconds": _NULLABLE_INT,
        "summary": {"type": "string"},
    },
}

_DRIFT_COMPONENT = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "running_version": _NULLABLE_STRING,
        "running_sha": _NULLABLE_STRING,
        "desired_ref": _NULLABLE_STRING,
        "desired_origin": _NULLABLE_STRING,
        "verdict": {"enum": ["in_sync", "divergent", "unknown"]},
    },
}

DATA_SCHEMAS: dict[str, dict] = {
    "status": {
        "type": "object",
        "required": ["identity", "runtime_health", "supervisor", "gateway"],
        "properties": {
            "identity": {
                "type": "object",
                "properties": {
                    "computer_id": _NULLABLE_STRING,
                    "instance_id": _NULLABLE_STRING,
                    "runtime_ref": _NULLABLE_STRING,
                },
            },
            "runtime_health": _NULLABLE_STRING,
            "supervisor": {
                "type": "object",
                "required": ["unit", "live_unit_state"],
            },
            "gateway": {
                "type": "object",
                "required": ["unit", "live_unit_state"],
            },
            "recent_events": {"type": "array"},
        },
    },
    "health": {
        "type": "object",
        "required": [
            "runtime_health",
            "supervisor_status",
            "gateway_status",
            "gateway_active_live",
            "demoted_by_live_check",
        ],
        "properties": {
            "runtime_health": {"type": "string"},
            "demoted_by_live_check": {"type": "boolean"},
            "gateway_active_live": _NULLABLE_BOOL,
            "plugin_check": {"type": ["object", "null"]},
            "gateway_recovery": {"type": "object"},
        },
    },
    "manifest show": {
        "type": "object",
        "required": [
            "running",
            "creation_spec",
            "last_acked_update",
            "desired_source",
            "admin_drift_authoritative",
            "desired_staleness",
        ],
        "properties": {
            "running": {"type": "object"},
            "creation_spec": {
                "type": "object",
                "required": ["path", "present"],
            },
            "last_acked_update": {
                "type": "object",
                "required": ["present"],
            },
            "desired_source": {"const": "on_box_last_known"},
            "admin_drift_authoritative": {"const": True},
            "desired_staleness": _DESIRED_STALENESS,
        },
    },
    "manifest drift": {
        "type": "object",
        "required": [
            "desired_source",
            "admin_drift_authoritative",
            "desired_staleness",
            "components",
            "drift_detected",
        ],
        "properties": {
            "desired_source": {"const": "on_box_last_known"},
            "admin_drift_authoritative": {"const": True},
            "desired_staleness": _DESIRED_STALENESS,
            "components": {
                "type": "object",
                "required": ["runtime", "plugin", "framework"],
                "properties": {
                    "runtime": _DRIFT_COMPONENT,
                    "plugin": _DRIFT_COMPONENT,
                    "framework": _DRIFT_COMPONENT,
                },
            },
            "drift_detected": _NULLABLE_BOOL,
        },
    },
    "whoami": {
        "type": "object",
        "required": [
            "computer_id",
            "instance_id",
            "hostname",
            "runtime_ref",
            "gce_metadata_available",
            "binding",
        ],
        "properties": {
            "hostname": {"type": "string"},
            "gce_metadata_available": {"type": "boolean"},
            "binding": {
                "type": "object",
                "required": ["bound"],
                "properties": {"bound": {"type": "boolean"}},
            },
        },
    },
}


def envelope_schema(command: str) -> dict:
    return {
        "type": "object",
        "required": [
            "schema",
            "command",
            "command_class",
            "generated_at",
            "state_as_of",
            "state_age_seconds",
            "supervisor_alive",
            "data",
        ],
        "properties": {
            "schema": {"const": "tinyhat_cli_v1"},
            "command": {"const": command},
            "command_class": {"enum": ["diagnose", "operate"]},
            "generated_at": {"type": "string"},
            "state_as_of": _NULLABLE_STRING,
            "state_age_seconds": {"type": ["integer", "number", "null"]},
            "supervisor_alive": _NULLABLE_BOOL,
            "data": DATA_SCHEMAS[command],
        },
    }


def validate_envelope(command: str, instance: Any) -> list[str]:
    if command not in DATA_SCHEMAS:
        return [f"$: unknown command {command!r}"]
    return validate(instance, envelope_schema(command))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in DATA_SCHEMAS:
        known = ", ".join(sorted(DATA_SCHEMAS))
        print(f"usage: python3 -m tinyhat_cli.schemas <command>  ({known})", file=sys.stderr)
        return 2
    try:
        instance = json.load(sys.stdin)
    except ValueError as exc:
        print(f"stdin is not JSON: {exc}", file=sys.stderr)
        return 1
    errors = validate_envelope(argv[0], instance)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"schema-valid: {argv[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
