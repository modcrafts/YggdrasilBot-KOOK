"""Skills: built-in tool bundles and user-defined instruction skills loaded by the chat agent."""

from collections.abc import Callable

from pydantic_ai.capabilities import AbstractCapability, Capability

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.skills import kook, stickers
from yggdrasil.runtime import CustomSkillInfo, snapshot
from yggdrasil.settings import Settings

SKILLS: dict[str, Callable[..., AbstractCapability[Deps]]] = {
    kook.ID: kook.capability,
    stickers.ID: stickers.capability,
}


def custom_capability(skill: CustomSkillInfo) -> Capability[Deps]:
    """Build a pydantic-ai :class:`Capability` from a dashboard-managed custom skill."""
    return Capability[Deps](
        id=skill.id,
        description=skill.description or skill.name,
        instructions=skill.instructions,
        tools=(),
        defer_loading=skill.defer_loading,
    )


def skill_names(settings: Settings, handler: str, channel_id: str | None) -> list[str]:
    """Resolve active built-in and custom skill IDs for a run."""
    configured = list(dict.fromkeys(settings.tools.skills))
    configured_set = set(configured)
    names: list[str] = []

    if handler == "chat" and channel_id is not None:
        names.extend(name for name in configured if name in SKILLS)

    seen = set(names)
    for skill in snapshot().custom_skills.values():
        if not skill.enabled or skill.id in seen:
            continue
        if handler not in skill.handlers:
            continue
        if channel_id is None and not skill.allow_private:
            continue
        if not skill.default_active and skill.id not in configured_set:
            continue
        names.append(skill.id)
        seen.add(skill.id)
    return names


def skills_for(names: list[str]) -> list[AbstractCapability[Deps]]:
    """Instantiate capabilities for resolved skill IDs (skipping deleted skills safely)."""
    snap = snapshot()
    out: list[AbstractCapability[Deps]] = []
    for name in names:
        if name in SKILLS:
            override = snap.builtin_skill_overrides.get(name)
            extra = override.extra_instructions if override is not None else ""
            out.append(SKILLS[name](extra_instructions=extra))
        elif (custom := snap.custom_skills.get(name)) is not None and custom.enabled:
            out.append(custom_capability(custom))
    return out
