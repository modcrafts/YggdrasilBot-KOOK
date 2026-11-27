"""Tests for adaptive KOOK delivery: ``(chn)`` channel tags, KMarkdown splitting, CardMessage, and ``quote``."""

import json

from tests.support.kook import FakeKook
from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.delivery.render import (
    UploadedPicture,
    _sanitize_invisible_modules,
    build_card_batches,
    choose_format,
    extract_message_anchor_ids,
    extract_thread_anchor_ids,
    kook_anchor_url,
    render_reply,
    split_kmarkdown,
    to_kmarkdown,
)
from yggdrasil.delivery.send import send_reply
from yggdrasil.kook.model import KookError
from yggdrasil.settings import InteractionSettings


def test_to_kmarkdown_converts_channel_tags_in_chinese_prose_and_preserves_code_and_urls():
    channels = {"闲聊": "1001", "技术讨论": "1002", "dev": "1003", "题外故事": "1004"}
    text = (
        "刚才在#闲聊里提到过，详情去 #技术讨论 看看，也可以看#dev频道，还有 `#题外故事`。\n"
        "链接 https://example.com/#闲聊 不要改，行内代码 `print('#闲聊')` 不要改。\n"
        "```python\n# 闲聊\nx = '#闲聊'\n```"
    )
    out = to_kmarkdown(text, channel_map=channels)
    assert "刚才在(chn)1001(chn)里提到过" in out
    assert "去 (chn)1002(chn) 看看" in out
    assert "看(chn)1003(chn)频道" in out
    assert "还有 (chn)1004(chn)。" in out
    assert "https://example.com/#闲聊" in out
    assert "`print('#闲聊')`" in out
    assert "x = '#闲聊'" in out


def test_split_kmarkdown_keeps_code_fences_balanced():
    code = "```python\n" + "\n".join(f"x_{i} = {i}" for i in range(60)) + "\n```"
    chunks = split_kmarkdown(code, limit=240)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.count("```") % 2 == 0


def test_choose_format_adaptive_and_override():
    cfg = InteractionSettings()
    short = BotReply(mode="reply", sections=[Section(content="没问题，已经记下了。")])
    assert choose_format(short, cfg, picture_count=0) == "kmarkdown"
    assert choose_format(short, InteractionSettings(reply_style="card"), picture_count=0) == "card"

    structured = BotReply(
        mode="reply",
        title="排查结论",
        sections=[Section(heading="原因", content="连接池耗尽"), Section(heading="修复", content="调大上限")],
        sources=[SourceRef(label="文档", url="https://example.com")],
    )
    assert choose_format(structured, cfg, picture_count=0) == "card"
    assert choose_format(structured, InteractionSettings(reply_style="kmarkdown"), picture_count=0) == "kmarkdown"

    with_two_imgs = BotReply(mode="reply", sections=[Section(content="看图")])
    assert choose_format(with_two_imgs, cfg, picture_count=2) == "card"


def test_build_card_batches_valid_json_with_metadata_and_sources():
    reply = BotReply(
        mode="reply",
        title="分析结果",
        sections=[Section(heading="结论", content="在#闲聊里确认了方案", images=["img#1"])],
        sources=[SourceRef(label="KOOK 文档", url="https://developer.kookapp.cn")],
    )
    batches = build_card_batches(
        reply,
        "gpt-5 · 1.2s",
        InteractionSettings(),
        images={"img#1": UploadedPicture(url="https://img.kookapp.cn/assets/1.png", caption="截图")},
        channel_map={"闲聊": "1001"},
    )
    assert len(batches) == 1
    cards = batches[0]
    assert isinstance(cards, list)
    assert cards[0]["type"] == "card"
    modules = cards[0]["modules"]
    types = [m["type"] for m in modules]
    assert "header" in types
    assert "container" in types
    assert "context" in types
    serialized = json.dumps(cards, ensure_ascii=False)
    assert "(chn)1001(chn)" in serialized
    assert "https://img.kookapp.cn/assets/1.png" in serialized
    assert "gpt-5 · 1.2s" in serialized


async def test_send_reply_quotes_trigger_message(recorded):
    kook = FakeKook()
    reply = BotReply(mode="reply", sections=[Section(content="去#闲聊看一眼")])
    cfg = InteractionSettings()

    msg_id = await send_reply(
        kook,
        channel_id="ch-1",
        guild_id="g-1",
        user_id="u-1",
        reply_to="trig-uuid",
        reply=reply,
        meta_text=None,
        cfg=cfg,
        self_id="bot-1",
        nickname="Ygg",
        channel_map={"闲聊": "1001"},
    )
    assert msg_id == "msg-1"
    assert len(kook.sent) == 1
    assert kook.sent[0].kind == "kmarkdown"
    assert kook.sent[0].quote == "trig-uuid"
    assert "(chn)1001(chn)" in kook.sent[0].content
    assert len(recorded) == 1

    # reply_to=None omits quote
    await send_reply(
        kook,
        channel_id="ch-1",
        guild_id="g-1",
        user_id="u-1",
        reply_to=None,
        reply=reply,
        meta_text=None,
        cfg=cfg,
        self_id="bot-1",
        nickname="Ygg",
    )
    assert kook.sent[1].quote is None


async def test_invisible_card_sanitizes_modules_and_falls_back_to_primary_on_40000(recorded):
    cfg = InteractionSettings(reply_style="invisible")
    short = BotReply(mode="reply", sections=[Section(content="无边框卡片内容")])
    assert choose_format(short, cfg, picture_count=0) == "card"

    sanitized = _sanitize_invisible_modules(
        [
            {"type": "header", "text": {"type": "plain-text", "content": "标题"}},
            {
                "type": "section",
                "text": {"type": "kmarkdown", "content": "正文"},
                "mode": "right",
                "accessory": {"type": "button"},
            },
            {"type": "file", "src": "https://img.kookapp.cn/a.pdf"},
            {"type": "countdown", "mode": "second"},
            {"type": "divider"},
            {"type": "context", "elements": [{"type": "plain-text", "content": "meta"}]},
        ]
    )
    assert [m["type"] for m in sanitized] == ["header", "section", "divider", "context"]
    assert "accessory" not in sanitized[1]
    assert "mode" not in sanitized[1]

    batches = build_card_batches(short, "gpt-5 · 0.5s", cfg, theme="invisible")
    assert batches[0][0]["theme"] == "invisible"

    # Simulate KOOK rejecting invisible card with code=40000 -> automatic fallback to primary card
    class RejectInvisibleKook(FakeKook):
        async def send_card(self, *, channel_id, user_id, card_json, quote=None, chat_code=None) -> str:
            parsed = json.loads(card_json)
            if parsed[0]["theme"] == "invisible":
                raise KookError("invalid card", code=40000)
            return await super().send_card(
                channel_id=channel_id, user_id=user_id, card_json=card_json, quote=quote, chat_code=chat_code
            )

    kook = RejectInvisibleKook()
    mid = await send_reply(
        kook,
        channel_id="ch-1",
        guild_id="g-1",
        user_id="u-1",
        reply_to="trig-1",
        reply=short,
        meta_text="gpt-5 · 0.5s",
        cfg=cfg,
        self_id="bot-1",
        nickname="Ygg",
        theme="invisible",
    )
    assert mid == "msg-1"
    assert len(kook.sent) == 1
    assert json.loads(kook.sent[0].content)[0]["theme"] == "primary"
    assert len(recorded) == 1


def test_message_and_thread_anchors_expand_to_bare_kook_anchor_urls():
    msg_url = kook_anchor_url("2827730975763212", "1223237271137726", "a46693d3-e5a6-420d-b651-981ae282e132")
    thread_url = kook_anchor_url("2827730975763212", "3148909618967474", "153610525312287232")
    post_url = kook_anchor_url("2827730975763212", "3148909618967474", "153610525312287232", "153674495008179200")

    reply = BotReply(
        mode="reply",
        sections=[
            Section(
                content=(
                    "原消息在 #msg:42 ，也可以看 `#msg:42` 或 [原话](#msg:42)。\n"
                    "帖子见 #thread:153610525312287232 ，具体回复在 `#thread:153610525312287232/153674495008179200`。\n"
                    "```text\n#msg:42 #thread:153610525312287232\n```"
                )
            )
        ],
        sources=[
            SourceRef(label="#题外故事 · NCG爆笑手术Top榜（仍在更新中）", url="#msg:42"),
            SourceRef(label="[置顶] *重要* (测试) `tag`", url="#thread:153610525312287232/153674495008179200"),
            SourceRef(label="#题外故事 讨论记录"),
        ],
    )
    assert extract_message_anchor_ids(reply) == {42}
    assert extract_thread_anchor_ids(reply) == {"153610525312287232"}

    plan = render_reply(
        reply,
        None,
        InteractionSettings(reply_style="kmarkdown"),
        channel_map={"题外故事": "3148909618967474"},
        message_anchor_map={42: msg_url},
        thread_channel_map={"153610525312287232": ("2827730975763212", "3148909618967474")},
    )
    kmd = plan.kmarkdown
    # Bare anchor tags expand to bare KOOK anchor URLs (native KOOK chip); explicit [label](#msg:42) keeps label
    assert f"原消息在 {msg_url} ，也可以看 {msg_url} 或 [原话]({msg_url})。" in kmd
    assert f"帖子见 {thread_url} ，具体回复在 {post_url}。" in kmd
    # Sources with URL wrap the whole escaped label inside [label](url) without nesting (chn)
    assert f"- [#题外故事 · NCG爆笑手术Top榜（仍在更新中）]({msg_url})" in kmd
    assert rf"- [\[置顶\] \*重要\* \(测试\) \`tag\`]({post_url})" in kmd
    # Sources without URL still convert #频道名 -> (chn)
    assert "- (chn)3148909618967474(chn) 讨论记录" in kmd
    # Code fence stays untouched
    assert "```text\n#msg:42 #thread:153610525312287232\n```" in kmd
