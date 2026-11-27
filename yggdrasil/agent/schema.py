"""Structured output schemas for the chat, tldr, and ops agents."""

from typing import Literal

from pydantic import BaseModel, Field


class Section(BaseModel):
    heading: str | None = Field(default=None, description="可选的小节标题（长回答分节时使用，简短回复留空）")
    content: str = Field(
        description="正文，支持标准 Markdown（加粗、列表、行内代码、代码块、链接）；按人类阅读习惯自然分段"
    )
    images: list[str] = Field(
        default_factory=list,
        description="紧跟在这一节正文后面展示的图片句柄（img#N），按顺序；render_*、show() 生成的图"
        "和聊天、网页里的图都行。整条回复最多 4 张；没写进来的图不会发",
    )


class SourceRef(BaseModel):
    label: str = Field(description="来源名称，如网页标题、文件名、#频道名 原消息")
    url: str | None = Field(
        default=None,
        description=(
            "完整链接；指向历史消息或帖子时可直接填 #msg:消息编号、#thread:帖子ID "
            "或 #thread:帖子ID/评论post_id（系统自动转为 KOOK 原生锚点链接）"
        ),
    )


class BotReply(BaseModel):
    """Structured answer of the chat / tldr agents.

    The delivery renderer adaptively picks concise KMarkdown or structured CardMessage based on length,
    structure, images, and sources so the model focuses on clear writing.
    """

    mode: Literal["reply", "silent", "done"] = Field(
        description="reply：发出回复（由发送层根据内容长度、分节、图片与来源自适应选择简洁文本或卡片消息）；"
        "silent：不回复（不是在跟你说话、没有实际内容）；"
        "done：已经用操作类工具完成了请求（比如撤回），不再发消息"
    )
    style: Literal["auto", "text", "card"] = Field(
        default="auto",
        description="可选排版提示：默认 auto 自动选择最易读的样式；简短闲聊可用 text，结构化报告可用 card",
    )
    title: str | None = Field(default=None, description="长回答或 TL;DR 的主题标题（≤30 字；简短一句回复可留空）")
    sections: list[Section] = Field(default_factory=list, description="mode=reply 时的各节内容")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")
    silent_reason: str | None = Field(
        default=None, description="mode=silent 时一句话说明为什么不回（只记日志，不发出去）"
    )
    silent_reaction: str | None = Field(
        default=None,
        description=(
            "仅 mode=silent 且确实有必要用单个表情轻量回应时填一个 Unicode 表情"
            "（如 👍、😂、🙏）代替默认沉默表情；普通无关闲聊、刷屏、引战务必留空"
        ),
    )

    def full_text(self) -> str:
        parts: list[str] = []
        for s in self.sections:
            chunk = f"{s.heading}\n{s.content}" if s.heading else s.content
            if chunk.strip():
                parts.append(chunk.strip())
        return "\n\n".join(parts)


class OpsReply(BaseModel):
    """Structured answer of the ops agent (``yggdrasil.ops.agent``)."""

    mode: Literal["done", "reply"] = Field(
        description="done：已用工具完成操作，不发消息；reply：回一段简短说明（查询结果、失败原因、需要澄清时）"
    )
    text: str | None = Field(default=None, description="mode=reply 时发出的文字，简短清晰，可使用基础 Markdown")
