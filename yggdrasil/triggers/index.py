"""Compiled, immutable trigger index built from ``command_triggers``.

Four-level inheritance:
- Channel chats resolve candidate rules along ``channel -> category -> guild -> global``.
  If an inner scope defines any rule for a handler, outer rules for that same handler are ignored.
- Direct messages resolve ``private -> global`` (global rules restricted to ``admin.*`` / ``privacy.*``).
- Candidates are tried in ascending ``(priority, id)``; the first match wins.
"""

import re
from dataclasses import dataclass
from functools import cached_property

from yggdrasil.runtime import Snapshot, TriggerRule

SCOPES = ("global", "guild", "category", "channel", "private")
MATCH_TYPES = ("keyword_prefix", "keyword_exact", "regex", "mention", "reply_to_bot", "name_mention")
TARGETS = ("quoted_or_self", "self", "quoted")
ROLES = ("member", "admin", "owner", "superuser")


@dataclass(frozen=True, slots=True)
class MatchInput:
    scope: str  # channel | private
    channel_id: str | None = None
    category_id: str | None = None
    guild_id: str | None = None
    text: str = ""
    to_me: bool = False
    replied_to_bot: bool = False
    role: str = "member"
    ops_authorized: bool = False


@dataclass(frozen=True, slots=True)
class Resolved:
    handler: str
    rule_id: int
    args: str
    captures: tuple[str, ...]
    target: str
    cooldown_s: int
    named: bool


@dataclass(frozen=True)
class _Compiled:
    rule: TriggerRule
    regexes: tuple[re.Pattern[str], ...]


def _role_ok(rule: TriggerRule, role: str) -> bool:
    if not rule.allowed_roles or role == "superuser":
        return True
    allowed = set(rule.allowed_roles)
    return role in allowed or (role == "owner" and "admin" in allowed)


class TriggerIndex:
    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap
        compiled: list[_Compiled] = []
        for rule in snap.triggers:
            regexes = ()
            if rule.match_type == "regex":
                regexes = tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in rule.patterns)
            compiled.append(_Compiled(rule, regexes))
        self._rules = compiled

    @cached_property
    def _by_scope(self) -> dict[tuple[str, str | None], list[_Compiled]]:
        out: dict[tuple[str, str | None], list[_Compiled]] = {}
        for c in self._rules:
            sid = None if c.rule.scope in ("global", "private") else c.rule.scope_id
            out.setdefault((c.rule.scope, sid), []).append(c)
        return out

    def _candidates(self, inp: MatchInput) -> list[_Compiled]:
        if inp.scope == "private":
            chain: list[tuple[str, str | None]] = [("private", None), ("global", None)]
        else:
            chain = []
            if inp.channel_id:
                chain.append(("channel", inp.channel_id))
            if inp.category_id:
                chain.append(("category", inp.category_id))
            if inp.guild_id:
                chain.append(("guild", inp.guild_id))
            chain.append(("global", None))

        selected: list[_Compiled] = []
        overridden: set[str] = set()
        for scope_name, sid in chain:
            layer = self._by_scope.get((scope_name, sid), [])
            if inp.scope == "private" and scope_name == "global":
                layer = [c for c in layer if c.rule.handler.startswith(("admin.", "privacy."))]
            kept = [c for c in layer if c.rule.handler not in overridden]
            selected.extend(kept)
            overridden.update(c.rule.handler for c in kept)
        return sorted(selected, key=lambda c: (c.rule.priority, c.rule.id))

    def names(self, inp: MatchInput) -> list[str]:
        persona = self._snap.settings(
            channel_id=inp.channel_id, guild_id=inp.guild_id, category_id=inp.category_id
        ).persona
        return [n for n in [persona.name, *persona.aliases] if n]

    def match(self, inp: MatchInput) -> Resolved | None:
        text = inp.text.strip()
        lowered = text.lower()
        named = any(n.lower() in lowered for n in self.names(inp))
        mentioned = inp.to_me or named
        for c in self._candidates(inp):
            rule = c.rule
            if rule.require_mention and not mentioned:
                continue
            if not (_role_ok(rule, inp.role) or (rule.handler == "admin.ops" and inp.ops_authorized)):
                continue
            hit = self._match_rule(c, text, lowered, inp, named)
            if hit is None:
                continue
            args, captures = hit
            if rule.arg_template:
                try:
                    args = rule.arg_template.format(*captures, rest=args.strip())
                except (IndexError, KeyError):
                    pass
            return Resolved(rule.handler, rule.id, args.strip(), captures, rule.target, rule.cooldown_s, named)
        return None

    @staticmethod
    def _match_rule(
        c: _Compiled, text: str, lowered: str, inp: MatchInput, named: bool
    ) -> tuple[str, tuple[str, ...]] | None:
        rule = c.rule
        match rule.match_type:
            case "keyword_prefix":
                for p in rule.patterns:
                    if lowered.startswith(p.lower()):
                        return text[len(p) :], ()
            case "keyword_exact":
                for p in rule.patterns:
                    if lowered == p.lower():
                        return "", ()
            case "regex":
                for rx in c.regexes:
                    if m := rx.search(text):
                        rest = (text[: m.start()] + text[m.end() :]).strip()
                        return rest, tuple(g or "" for g in m.groups())
            case "mention":
                if inp.to_me:
                    return text, ()
            case "reply_to_bot":
                if inp.replied_to_bot:
                    return text, ()
            case "name_mention":
                if named:
                    return text, ()
        return None


_index: TriggerIndex | None = None


def current() -> TriggerIndex:
    from yggdrasil.runtime import snapshot

    global _index
    if _index is None or _index._snap is not snapshot():
        _index = TriggerIndex(snapshot())
    return _index
