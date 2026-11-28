from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.client import Kook
from yggdrasil.settings import Settings
from yggdrasil.tools.azuriom import CallerBindingCache
from yggdrasil.transcript.notes import TraceNotes

if TYPE_CHECKING:
    from yggdrasil.agent.bridge import RunBridge
    from yggdrasil.sandbox import Session

_PREFIX = {"image": "img", "file": "file", "card": "card", "url": "url"}
_IDENTITY_KEYS = ("blob", "url")


def _identity(data: dict[str, Any]) -> tuple[str, str] | None:
    return next(((k, str(data[k])) for k in _IDENTITY_KEYS if data.get(k)), None)


@dataclass(slots=True)
class Handle:
    key: str
    kind: str  # image | file | card | url
    data: dict[str, Any]


@dataclass
class Handles:
    """Short per-run handles (img#1, file#1, card#1, url#1) for rich content shown to the model."""

    items: dict[str, Handle] = field(default_factory=dict)
    _counters: Counter[str] = field(default_factory=Counter)

    def add(self, kind: str, data: dict[str, Any]) -> str | None:
        prefix = _PREFIX.get(kind)
        if prefix is None:
            return None
        ident = _identity(data)
        for h in self.items.values():
            if h.kind == kind and ident is not None and _identity(h.data) == ident:
                return h.key
        self._counters[prefix] += 1
        key = f"{prefix}#{self._counters[prefix]}"
        self.items[key] = Handle(key, kind, data)
        return key

    def register(self, segments: list[dict[str, Any]], *, urls: bool = False) -> str:
        norm = normalize(segments)
        keys = [key for att in norm.attachments if (key := self.add(att.kind, att.data))]
        if urls:
            keys += [key for url in norm.urls if (key := self.add("url", {"url": url}))]
        return f" ({', '.join(dict.fromkeys(keys))})" if keys else ""

    def get(self, key: str) -> Handle | None:
        return self.items.get(key.strip())

    def dump(self) -> dict[str, Any]:
        return {
            "items": [{"key": h.key, "kind": h.kind, "data": h.data} for h in self.items.values()],
            "counters": dict(self._counters),
        }

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Handles":
        items = {i["key"]: Handle(i["key"], i["kind"], i["data"]) for i in raw.get("items", [])}
        return cls(items, Counter(raw.get("counters", {})))


@dataclass
class Deps:
    kook: Kook
    channel_id: str | None
    guild_id: str | None
    user_id: str
    settings: Settings
    handles: Handles
    task: str  # chat | tldr
    recall_target: str | None = None
    self_id: str = ""
    bot_nickname: str = ""
    chat_code: str | None = None
    actions: list[str] = field(default_factory=list)
    tool_counts: Counter[str] = field(default_factory=Counter)
    tool_seconds: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    vision_tokens: list[tuple[str, int, int]] = field(default_factory=list)
    images: list[tuple[bytes, str]] = field(default_factory=list)
    sandbox: "Session | None" = None
    trigger_message_id: str | None = None
    invocation_id: str | None = None
    request_text: str = ""
    notes: TraceNotes = field(default_factory=TraceNotes)
    made_images: dict[str, bytes] = field(default_factory=dict)
    text_only: bool = False
    bridge: "RunBridge | None" = None
    role_ids: tuple[str, ...] = ()
    forum_thread_channels: dict[str, str] = field(default_factory=dict)  # thread_id -> forum channel_id
    caller_binding: CallerBindingCache = field(default_factory=CallerBindingCache)
