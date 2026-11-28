"""Four-level (global -> guild -> category -> channel) KOOK role & tool access control.

Superusers always bypass scope/role restrictions. For everyone else:

1. **Bot trigger access** (:func:`can_use_bot`):
   - ``access.denied_roles``: any matching KOOK role denies access (highest precedence).
   - ``access.role_rules[role_id].allow_bot``: explicit ``False`` denies; explicit ``True`` allows
     (can grant a VIP role access even when the scope's ``allow_bot`` is ``False`` or ``allowed_roles`` is set).
   - ``access.allow_bot``: scope-level master switch for whether members can trigger the bot.
   - ``access.allowed_roles``: when non-empty, restricts triggers to holders of any listed KOOK role
     (plus guild owner/admins when ``access.allow_guild_admins`` is True).

2. **Effective tool list** (:func:`effective_tools`):
   - Starts from the 4-level inherited ``settings.tools.enabled``.
   - Applies all matching ``access.role_rules[role_id]``: appends ``tools_allow`` (deduped) and removes
     ``tools_deny`` (deny always wins over allow).
"""

from collections.abc import Collection

from yggdrasil.kook.model import Incoming
from yggdrasil.settings import RoleAccessRule, Settings


def _role_id_strings(incoming: Incoming) -> tuple[str, ...]:
    return tuple(str(r) for r in incoming.sender.role_ids)


def _matched_rules(incoming: Incoming, settings: Settings) -> list[RoleAccessRule]:
    rules = settings.access.role_rules
    if not rules or not incoming.sender.role_ids:
        return []
    return [rules[rid] for rid in _role_id_strings(incoming) if rid in rules]


def can_use_bot(incoming: Incoming, settings: Settings, *, role: str) -> tuple[bool, str | None]:
    """Return ``(allowed, reason_if_blocked)`` for whether ``incoming`` may invoke the bot."""
    if role == "superuser":
        return True, None

    cfg = settings.access
    holder = set(_role_id_strings(incoming))

    denied = {r.strip() for r in cfg.denied_roles if r.strip()}
    if holder & denied:
        return False, "denied_role"

    matched = _matched_rules(incoming, settings)
    if any(r.allow_bot is False for r in matched):
        return False, "role_rule_denied"
    if any(r.allow_bot is True for r in matched):
        return True, None

    if not cfg.allow_bot:
        return False, "scope_bot_disabled"

    allowed = {r.strip() for r in cfg.allowed_roles if r.strip()}
    if allowed:
        if cfg.allow_guild_admins and role in ("owner", "admin"):
            return True, None
        if holder & allowed:
            return True, None
        return False, "role_not_allowed"

    return True, None


def effective_tools(settings: Settings, incoming: Incoming, *, role: str) -> list[str]:
    """Effective enabled tool names (including ``sandbox``) after applying scope + KOOK role overrides."""
    base: list[str] = list(settings.tools.enabled)
    if role == "superuser":
        return base

    matched = _matched_rules(incoming, settings)
    if not matched:
        return base

    seen = set(base)
    out = list(base)
    for rule in matched:
        for t in rule.tools_allow:
            if t not in seen:
                seen.add(t)
                out.append(t)

    deny: set[str] = set()
    for rule in matched:
        deny.update(rule.tools_deny)
    if deny:
        out = [t for t in out if t not in deny]
    return out


def can_bypass_channel_privacy(settings: Settings, role_ids: Collection[str]) -> bool:
    """Whether any of the speaker's KOOK roles grants ``bypass_channel_privacy`` in ``settings``."""
    rules = settings.access.role_rules
    if not rules or not role_ids:
        return False
    return any(rules[rid].bypass_channel_privacy for raw in role_ids if (rid := str(raw).strip()) in rules)


def can_lookup_other_identities(
    settings: Settings, user_id: str, role_ids: Collection[str], superusers: Collection[str]
) -> bool:
    """Whether a speaker may look up other members' Azuriom bindings."""
    if user_id in superusers:
        return True
    rules = settings.access.role_rules
    return any(rules[rid].azuriom_lookup_others for rid in role_ids if rid in rules)
