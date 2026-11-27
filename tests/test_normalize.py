"""Tests for KOOK segment normalization (mentions, roles, emojis, images, files, cards)."""

from typing import Any

from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook import segments as seg


def _seg(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data}


def test_every_kook_segment_type_reads_as_placeholder_text():
    segments = [
        seg.reply("orig-uuid"),
        seg.mention("u-1", "阿明"),
        seg.mention_all(),
        seg.mention_role("10", "管理员"),
        seg.text(" 去 #技术讨论 看 "),
        seg.emoji("g-1/e1", "doge", guild_id="g-1"),
        seg.image("https://img.kookapp.cn/i.png"),
        seg.image("https://img.kookapp.cn/s.gif", name="滑稽", sticker=True),
        seg.image("https://img.kookapp.cn/s2.gif", name="", sticker=True),
        _seg("audio", url="https://img.kookapp.cn/a.mp3", duration=3),
        _seg("video", url="https://img.kookapp.cn/v.mp4", duration=0),
        seg.file("https://img.kookapp.cn/a.xlsx", file_name="a.xlsx", file_size=10),
        seg.card([], "卡片摘要 https://a.b"),
    ]
    norm = normalize(segments)
    assert "@u-1" in norm.text
    assert "@全体成员" in norm.text
    assert "@管理员" in norm.text
    assert "#技术讨论" in norm.text
    assert "[表情:doge]" in norm.text
    assert "[图片]" in norm.text
    assert "[表情包:滑稽]" in norm.text
    assert "[语音]" in norm.text
    assert "[视频]" in norm.text
    assert "[文件:a.xlsx]" in norm.text
    assert "卡片摘要 https://a.b" in norm.text
    assert norm.reply_to == "orig-uuid"
    assert norm.mentions == ["u-1"]
    assert "https://a.b" in norm.urls
    # Card without explicit url does not pollute attachments with a redundant card#N handle
    assert all(a.kind != "card" for a in norm.attachments)


def test_a_mention_renders_through_the_callback_and_owns_its_space():
    segments = [seg.mention("u-2"), seg.text(" 你好")]
    assert normalize(segments, mention=lambda uid: f"小红({uid})").text == "@小红(u-2) 你好"
    assert normalize(segments).text == "@u-2  你好"


def test_a_link_stops_before_the_sentence_punctuation_after_it():
    text = "看 https://example.com/new-spline/; 还有 https://example.com/a;b=1?q=2. 和 https://example.com/x!"
    norm = normalize([seg.text(text)])
    assert norm.urls == ["https://example.com/new-spline/", "https://example.com/a;b=1?q=2", "https://example.com/x"]


def test_self_repeated_kook_anchor_links_collapse_without_losing_url_handle():
    msg_url = (
        "https://www.kookapp.cn/direct/anchor/2827730975763212/1701540030305840/3f381b6c-34e2-4113-b852-aedb88a08864"
    )
    thread_url = "https://www.kookapp.cn/direct/anchor/2827730975763212/3148909618967474/153610525312287232"
    post_url = (
        "https://www.kookapp.cn/direct/anchor/2827730975763212/3148909618967474/153610525312287232/153674495008179200"
    )
    web_url = "https://example.com/doc"
    norm = normalize(
        [
            seg.text(
                f"看 [{msg_url}]({msg_url}) 和 [{thread_url}]({thread_url}) "
                f"还有 [{post_url}]({post_url}) 以及 [{web_url}]({web_url})"
            )
        ]
    )
    assert norm.text == f"看 [KOOK消息链接] 和 [KOOK帖子链接] 还有 [KOOK帖子回复链接] 以及 {web_url}"
    assert norm.urls == [msg_url, thread_url, post_url, web_url]
