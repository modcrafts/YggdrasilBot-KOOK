"""Multi-server HTTP long-polling bridge for Garry's Mod Lua servers and NCG queue operations."""

from yggdrasil.gmod.bridge import (
    HEARTBEAT_TTL_S,
    GModCallResult,
    GModError,
    GModHeartbeat,
    GModRemoteError,
    GModServerNotFound,
    GModServerOffline,
    GModTimeout,
    call,
    get_heartbeat,
    pop_jobs,
    record_heartbeat,
    submit_result,
)
from yggdrasil.gmod.queue import (
    grant_singlepass,
    letin_player,
    list_queue,
    lookup_queue,
    normalize_steamid,
    resolve_server_id,
)

__all__ = [
    "HEARTBEAT_TTL_S",
    "GModCallResult",
    "GModError",
    "GModHeartbeat",
    "GModRemoteError",
    "GModServerNotFound",
    "GModServerOffline",
    "GModTimeout",
    "call",
    "get_heartbeat",
    "grant_singlepass",
    "letin_player",
    "list_queue",
    "lookup_queue",
    "normalize_steamid",
    "pop_jobs",
    "record_heartbeat",
    "resolve_server_id",
    "submit_result",
]
