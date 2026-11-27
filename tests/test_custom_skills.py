"""Tests for user-defined custom skills and built-in skill extra instructions."""

from collections.abc import Callable

from pydantic_ai.capabilities import Capability

from yggdrasil.agent.skills import skill_names, skills_for
from yggdrasil.runtime import BuiltinSkillOverrideInfo, CustomSkillInfo, Snapshot
from yggdrasil.settings import Settings


def test_custom_skill_resolution_and_capability_building(snapshot: Callable[..., Snapshot]) -> None:
    faq = CustomSkillInfo(
        id="server-faq",
        name="服务器 FAQ",
        description="问服务器规则时加载",
        instructions="1. 友善交流\n2. 禁止刷屏",
        defer_loading=True,
        handlers=("chat",),
        allow_private=True,
        enabled=True,
        default_active=True,
    )
    tldr_only = CustomSkillInfo(
        id="tldr-style",
        name="摘要规范",
        description="TLDR 输出结构",
        instructions="必须分条总结",
        defer_loading=False,
        handlers=("tldr",),
        allow_private=False,
        enabled=True,
        default_active=True,
    )
    opt_in = CustomSkillInfo(
        id="dev-ops",
        name="运维手册",
        description="仅指定频道启用",
        instructions="先查日志",
        defer_loading=True,
        handlers=("chat",),
        allow_private=False,
        enabled=True,
        default_active=False,
    )
    disabled = CustomSkillInfo(
        id="disabled-skill",
        name="已停用",
        description="不应加载",
        instructions="无",
        enabled=False,
        default_active=True,
    )

    snapshot(
        custom_skills={s.id: s for s in (faq, tldr_only, opt_in, disabled)},
        builtin_skill_overrides={"kook": BuiltinSkillOverrideInfo(extra_instructions="本服发帖请使用简体中文。")},
    )

    # Channel chat gets built-ins + default_active chat custom skills (not opt-in / disabled / tldr-only).
    chat_names = skill_names(Settings(), "chat", "ch-1")
    assert chat_names == ["kook", "stickers", "server-faq"]

    # Private chat skips built-ins (channel_id is None) and channel-only skills, keeps allow_private custom skills.
    assert skill_names(Settings(), "chat", None) == ["server-faq"]

    # TLDR handler receives tldr custom skills.
    assert skill_names(Settings(), "tldr", "ch-1") == ["tldr-style"]
    assert skill_names(Settings(), "tldr", None) == []

    # Opt-in skill activates once added to settings.tools.skills.
    scoped = Settings()
    scoped.tools.skills.append("dev-ops")
    assert "dev-ops" in skill_names(scoped, "chat", "ch-1")

    # Instantiate capabilities and verify built-in extra instructions + custom skill flags + deleted-id resilience.
    caps = skills_for(["kook", "server-faq", "tldr-style", "deleted-skill"])
    assert len(caps) == 3
    by_id = {c.id: c for c in caps if isinstance(c, Capability)}
    assert set(by_id) == {"kook", "server-faq", "tldr-style"}
    assert by_id["server-faq"].defer_loading is True
    assert by_id["server-faq"].description == "问服务器规则时加载"
    assert by_id["tldr-style"].defer_loading is False
    assert any("本服发帖请使用简体中文" in str(part) for part in by_id["kook"]._instructions)  # pyright: ignore[reportPrivateUsage]
