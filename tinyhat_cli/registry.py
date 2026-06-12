"""Static command registry + typed handler contract.

Every ``tinyhat`` command is one :class:`CommandSpec`: a name, a
command class (``diagnose`` or ``operate``), a unit category from the
closed mechanism set, and the per-command declarations the runner
enforces — privilege, side effect, risk tier, idempotency mode, and
the operation timeout. The registry is a static dict — no
auto-discovery, no scanning, no plugins. Adding a command means adding
a unit module and one entry here.

Handler contract::

    handler(ctx: CommandContext) -> dict      # the JSON `data` block
    render(data: dict) -> list[str]           # human output lines

Diagnose handlers never mutate runtime state, never take locks, and
never post to the platform. Operate handlers run under the global
command lock (``units/command_lock``) and record results through the
command spool. The entrypoint owns the envelope (freshness fields),
output sanitization, and rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# The closed unit-category set: a runtime command must be a mechanism,
# never a product capability (product capabilities are plugin tools /
# skills / platform API). The registry refuses entries outside the set.
ALLOWED_UNIT_CATEGORIES = frozenset(
    {
        "identity",
        "apply",
        "supervision",
        "recovery",
        "framework-compatibility",
        "diagnostics",
        "release-update-lifecycle",
    }
)

COMMAND_CLASSES = frozenset({"diagnose", "operate"})
RISK_TIERS = frozenset({"low", "moderate", "high"})
IDEMPOTENCY_MODES = frozenset({"not-applicable", "explicit-replay"})

# Mutating-command timeout default; specific commands declare tighter
# bounds (gateway restart: 120 s ≥ the 90 s readiness bound + the
# synchronous systemctl stop/start allowance — asserted in tests).
DEFAULT_OPERATE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class CommandContext:
    """Everything a handler may read: parsed args + one state snapshot."""

    args: Any
    snapshot: dict[str, Any]
    state: dict[str, Any]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command_class: str
    category: str
    privilege: str
    side_effect: bool
    summary: str
    handler: Callable[[CommandContext], dict[str, Any]]
    render: Callable[[dict[str, Any]], list[str]]
    risk_tier: str = "low"
    idempotency: str = "not-applicable"
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.category not in ALLOWED_UNIT_CATEGORIES:
            raise ValueError(
                f"command {self.name!r} declares category {self.category!r} "
                f"outside the closed set {sorted(ALLOWED_UNIT_CATEGORIES)}"
            )
        if self.command_class not in COMMAND_CLASSES:
            raise ValueError(
                f"command {self.name!r} declares class {self.command_class!r}"
            )
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(
                f"command {self.name!r} declares risk tier {self.risk_tier!r}"
            )
        if self.idempotency not in IDEMPOTENCY_MODES:
            raise ValueError(
                f"command {self.name!r} declares idempotency {self.idempotency!r}"
            )
        if self.command_class == "diagnose" and self.side_effect:
            raise ValueError(
                f"diagnose command {self.name!r} cannot declare side effects"
            )
        if self.command_class == "operate":
            if not self.side_effect:
                raise ValueError(
                    f"operate command {self.name!r} must declare its side effect"
                )
            if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
                raise ValueError(
                    f"operate command {self.name!r} must declare a positive "
                    "timeout_seconds"
                )


def build_registry() -> dict[str, CommandSpec]:
    """The static registry. Import units lazily to keep startup cheap."""
    from tinyhat_cli.units import gateway_restart, health, manifest, status, whoami

    return {
        "gateway restart": CommandSpec(
            name="gateway restart",
            command_class="operate",
            category="supervision",
            privilege="root",
            side_effect=True,
            risk_tier="moderate",
            idempotency="explicit-replay",
            timeout_seconds=gateway_restart.GATEWAY_RESTART_TIMEOUT_SECONDS,
            summary=(
                "lock-held gateway restart, driven to a terminal "
                "readiness verdict (succeeded/failed/timed_out)"
            ),
            handler=gateway_restart.run,
            render=gateway_restart.render,
        ),
        "status": CommandSpec(
            name="status",
            command_class="diagnose",
            category="diagnostics",
            privilege="root",
            side_effect=False,
            summary="one-look support answer: identity, health, gateway, plugin",
            handler=status.run,
            render=status.render,
        ),
        "health": CommandSpec(
            name="health",
            command_class="diagnose",
            category="diagnostics",
            privilege="root",
            side_effect=False,
            summary="the runtime-health projection, recomputed live",
            handler=health.run,
            render=health.render,
        ),
        "manifest show": CommandSpec(
            name="manifest show",
            command_class="diagnose",
            category="release-update-lifecycle",
            privilege="root",
            side_effect=False,
            summary="running versions + the box's own desired-state record",
            handler=lambda ctx: manifest.manifest_show(),
            render=manifest.render_show,
        ),
        "manifest drift": CommandSpec(
            name="manifest drift",
            command_class="diagnose",
            category="release-update-lifecycle",
            privilege="root",
            side_effect=False,
            summary="as-known-on-box drift verdicts (admin verdict authoritative)",
            handler=lambda ctx: manifest.manifest_drift(),
            render=manifest.render_drift,
        ),
        "whoami": CommandSpec(
            name="whoami",
            command_class="diagnose",
            category="identity",
            privilege="root",
            side_effect=False,
            summary="prove the GCE/Computer binding visibly and cheaply",
            handler=whoami.run,
            render=whoami.render,
        ),
    }
