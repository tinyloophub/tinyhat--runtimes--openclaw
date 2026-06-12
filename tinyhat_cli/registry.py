"""Static command registry + typed handler contract.

Every ``tinyhat`` command is one :class:`CommandSpec`: a name, a
command class (``diagnose`` now; ``operate`` arrives with the global
command lock), a unit category from the closed mechanism set, a
privilege declaration, a side-effect declaration, a handler and a
human renderer. The registry is a static dict — no auto-discovery, no
scanning, no plugins. Adding a command means adding a unit module and
one entry here.

Handler contract::

    handler(ctx: CommandContext) -> dict      # the JSON `data` block
    render(data: dict) -> list[str]           # human output lines

Handlers never mutate runtime state, never take locks, and never post
to the platform (diagnose class). The entrypoint owns the envelope
(freshness fields), output sanitization, and rendering.
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
        if self.command_class == "diagnose" and self.side_effect:
            raise ValueError(
                f"diagnose command {self.name!r} cannot declare side effects"
            )


def build_registry() -> dict[str, CommandSpec]:
    """The static registry. Import units lazily to keep startup cheap."""
    from tinyhat_cli.units import health, manifest, status, whoami

    return {
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
