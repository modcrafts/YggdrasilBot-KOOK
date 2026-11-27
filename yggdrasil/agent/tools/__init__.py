"""Agent tools; failures are returned as text so the model can adapt."""

from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai import RunContext, Tool

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools import attachments, history, images, web
from yggdrasil.agent.tools.base import ToolResult, chat_tool
from yggdrasil.core.log import LOGGER
from yggdrasil.kook.model import KookError
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

ALL_TOOLS: dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "read_url": web.read_url,
    "web_search": web.web_search,
    "read_x": web.read_x,
    "search_x": web.search_x,
    "read_file": attachments.read_file,
    "view_image": attachments.view_image,
    "check_provenance": attachments.check_provenance,
    "search_history": history.search_history,
    "get_messages_around": history.get_messages_around,
    "recent_chat": history.recent_chat,
    "lookup_glossary": history.lookup_glossary,
    "get_profile": history.get_profile,
    "render_mermaid": images.render_mermaid,
    "render_markdown": images.render_markdown,
    "render_html": images.render_html,
    "generate_image": images.generate_image,
}

SANDBOX = "sandbox"
_HANDLER_EXCLUDED: dict[str, frozenset[str]] = {
    "chat": frozenset(),
    "tldr": frozenset({"get_profile", "generate_image", "check_provenance", SANDBOX}),
}


def _sandbox_requested(enabled: Sequence[str], handler: str) -> bool:
    return SANDBOX in enabled and SANDBOX not in _HANDLER_EXCLUDED.get(handler, frozenset())


def suite_enabled(
    settings: Settings,
    handler: str,
    channel_id: str | None,
    *,
    global_settings: Settings,
    configured: bool,
    enabled_tools: Sequence[str] | None = None,
) -> bool:
    tools = settings.tools.enabled if enabled_tools is None else enabled_tools
    return (
        channel_id is not None
        and _sandbox_requested(tools, handler)
        and global_settings.tools.sandbox_enabled
        and configured
    )


def tool_names(enabled: Sequence[str], handler: str) -> list[str]:
    excluded = _HANDLER_EXCLUDED.get(handler, frozenset())
    return [name for name in enabled if name in ALL_TOOLS and name not in excluded]


def tools_for(names: list[str]) -> list[Tool[Deps]]:
    return [Tool(ALL_TOOLS[name], takes_ctx=True) for name in names]


@chat_tool("recall_message")
async def recall_message(ctx: RunContext[Deps]) -> str:
    """撤回/删除被回复的那条消息（你自己发的）。成功后选 mode=done，不要再发消息；失败就照实说明。"""
    target = ctx.deps.recall_target
    if target is None:
        raise ToolError("这次没有可以撤回的消息：请求者没有回复你的消息，或者没有权限让你撤回")
    LOGGER.info("recalling KOOK message {} in channel {}", target, ctx.deps.channel_id)
    try:
        await ctx.deps.kook.recall(channel_id=ctx.deps.channel_id, msg_id=target)
    except KookError as e:
        raise ToolError(f"KOOK 撤回消息失败：{e}") from e
    ctx.deps.actions.append(f"recall {target}")
    return "已撤回"


def action_tools() -> list[Tool[Deps]]:
    return [Tool(recall_message, takes_ctx=True)]
