"""What a run's tools showed the model, recorded for the console while the run happens."""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from yggdrasil.transcript.schema import (
    BridgeCall,
    ChatContextDisplay,
    ChatMessagesDisplay,
    Display,
    ImagesDisplay,
    ShownImage,
)


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    mime: str


def image_key(data: dict[str, Any]) -> str | None:
    key = data.get("blob") or data.get("url")
    return str(key) if key else None


def blob_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class TraceNotes:
    full: bool = True
    displays: dict[str, Display] = field(default_factory=dict)
    message_ids: set[int] = field(default_factory=set)
    image_keys: dict[str, str] = field(default_factory=dict)
    blobs: dict[str, Blob] = field(default_factory=dict)
    errors: set[str] = field(default_factory=set)
    bridge: dict[str, list[BridgeCall]] = field(default_factory=dict)
    context: ChatContextDisplay | None = None
    bot: tuple[str, str] | None = None  # (user_id, persona name)
    stored_blobs: set[str] = field(default_factory=set, init=False)  # committed by an earlier flush

    def show(self, call_id: str | None, display: Display) -> None:
        if self.full and call_id:
            self.displays[call_id] = display

    def messages(self, call_id: str | None, ids: list[int]) -> None:
        if not self.full or not call_id or not ids:
            return
        self.message_ids.update(ids)
        self.displays[call_id] = ChatMessagesDisplay(message_ids=ids)

    def image(self, data: bytes, mime: str, *, segment: dict[str, Any] | None = None) -> str | None:
        if not self.full or not data:
            return None
        sha = blob_id(data)
        self.blobs.setdefault(sha, Blob(data, mime))
        key = image_key(segment) if segment else None
        if key:
            self.image_keys[key] = sha
        return sha

    def shown(self, call_id: str | None, images: list[tuple[bytes, str]], mime: str = "image/png") -> None:
        if not self.full or not call_id or not images:
            return
        shown = [ShownImage(blob=sha, caption=caption) for data, caption in images if (sha := self.image(data, mime))]
        current = self.displays.get(call_id)
        if isinstance(current, ImagesDisplay):
            current.images += shown
        else:
            self.displays[call_id] = ImagesDisplay(images=shown)

    def failed(self, call_id: str | None) -> None:
        if call_id:
            self.errors.add(call_id)

    def bridged(self, call_id: str, call: BridgeCall) -> None:
        self.bridge.setdefault(call_id, []).append(call)
