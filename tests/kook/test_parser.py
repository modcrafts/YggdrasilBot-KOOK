"""Tests for KOOK event/REST parsing: KMarkdown tags, ``(chn)``, ``extra.quote``, CardMessage, bot flag, recalls."""

import json

from yggdrasil.kook.parser import (
    parse_card_or_kmarkdown,
    parse_forum_post,
    parse_forum_thread,
    parse_incoming,
    parse_observed,
    parse_recalled,
    parse_rest_message,
    parse_updated_message,
)


def test_parse_kmarkdown_mentions_channels_emojis_and_quote():
    event = {
        "channel_type": "GROUP",
        "type": 9,
        "target_id": "ch-1",
        "author_id": "u-alice",
        "content": "(met)bot-1(met) 去 (chn)ch-2(chn) 看看 (emj)doge(emj)[g-1/emo1]",
        "msg_id": "msg-new",
        "msg_timestamp": 1_700_000_000_000,
        "extra": {
            "type": 9,
            "guild_id": "g-1",
            "channel_name": "闲聊",
            "mention": ["bot-1"],
            "author": {
                "id": "u-alice",
                "username": "alice",
                "nickname": "爱丽丝",
                "roles": [10],
                "bot": False,
            },
            "kmarkdown": {
                "mention_part": [{"id": "bot-1", "username": "Ygg", "nickname": "小树"}],
                "mention_channel_part": [{"id": "ch-2", "name": "技术讨论"}],
            },
            "quote": {
                "id": "msg-orig-uuid",
                "rong_id": "legacy-rong-id",
                "type": 9,
                "content": "原消息内容",
                "create_at": 1_699_999_000_000,
                "author": {"id": "bot-1", "username": "Ygg", "bot": True},
            },
        },
    }
    inc = parse_incoming(event, self_id="bot-1", owner_id="u-owner", admin_role_ids={10})
    assert inc is not None
    assert inc.to_me is True
    assert inc.sender.role == "admin"
    assert inc.sender.is_bot is False
    # Quote prefers `id` over `rong_id` when neither is a UUID
    assert inc.reply is not None
    assert inc.reply.message_id == "msg-orig-uuid"
    assert inc.reply.sender.is_bot is True
    assert any(s["type"] == "reply" and s["data"]["message_id"] == "msg-orig-uuid" for s in inc.segments)
    # Channel tag resolved to #技术讨论 inside text segment
    assert any(s["type"] == "text" and "#技术讨论" in s["data"]["text"] for s in inc.segments)
    # Emoji parsed with id
    emj_segs = [s for s in inc.segments if s["type"] == "emoji"]
    assert len(emj_segs) == 1
    assert emj_segs[0]["data"]["id"] == "g-1/emo1"
    # Addressed text strips leading @bot
    assert "#技术讨论" in inc.text


def test_parse_quote_prefers_uuid_in_rong_id_over_short_quote_id():
    uuid = "1c4532f6-1234-5678-9abc-6347f410f91c"
    event = {
        "channel_type": "GROUP",
        "type": 9,
        "target_id": "ch-1",
        "author_id": "u-alice",
        "content": "撤回",
        "msg_id": "msg-2",
        "msg_timestamp": 1_700_000_000_000,
        "extra": {
            "type": 9,
            "guild_id": "g-1",
            "author": {"id": "u-alice", "username": "alice", "bot": False},
            "quote": {
                "id": "24819201",
                "rong_id": uuid,
                "type": 9,
                "content": "bot 的回答",
                "create_at": 1_699_999_000_000,
                "author": {"id": "bot-1", "username": "Ygg", "bot": True},
            },
        },
    }
    inc = parse_incoming(event, self_id="bot-1")
    assert inc is not None
    assert inc.reply is not None
    assert inc.reply.message_id == uuid

    # When `id` itself is the UUID and `rong_id` is absent, `id` is used.
    event["extra"]["quote"] = {
        "id": uuid,
        "type": 9,
        "content": "bot 的回答",
        "create_at": 1_699_999_000_000,
        "author": {"id": "bot-1", "username": "Ygg", "bot": True},
    }
    inc2 = parse_incoming(event, self_id="bot-1")
    assert inc2 is not None
    assert inc2.reply is not None
    assert inc2.reply.message_id == uuid


def test_parse_bot_sender_from_flag_and_reply_to_bot_sets_to_me():
    event = {
        "channel_type": "GROUP",
        "type": 9,
        "target_id": "ch-1",
        "author_id": "other-bot",
        "content": "我接着你的话问一句",
        "msg_id": "msg-bot-1",
        "msg_timestamp": 1_700_000_100_000,
        "extra": {
            "type": 9,
            "guild_id": "g-1",
            "author": {"id": "other-bot", "username": "Helper", "bot": True},
            "quote": {
                "id": "bot-prev",
                "type": 9,
                "content": "之前的回答",
                "author": {"id": "bot-1", "username": "Ygg", "bot": True},
            },
        },
    }
    inc = parse_incoming(event, self_id="bot-1")
    assert inc is not None
    assert inc.sender.is_bot is True
    assert inc.to_me is True


def test_parse_card_message_extracts_text_and_images():
    cards = [
        {
            "type": "card",
            "theme": "secondary",
            "modules": [
                {"type": "header", "text": {"type": "plain-text", "content": "今日快报"}},
                {"type": "section", "text": {"type": "kmarkdown", "content": "一切正常"}},
                {"type": "container", "elements": [{"type": "image", "src": "https://img.kookapp.cn/a.png"}]},
            ],
        }
    ]
    raw = {
        "id": "msg-card-1",
        "type": 10,
        "content": json.dumps(cards),
        "create_at": 1_700_000_200_000,
        "author": {"id": "u-1", "username": "bob"},
    }
    msg = parse_rest_message(raw, channel_id="ch-1", guild_id="g-1")
    assert msg.message_id == "msg-card-1"
    assert any(s["type"] == "card" and "今日快报" in s["data"]["plain_text"] for s in msg.segments)
    assert any(s["type"] == "image" and s["data"]["url"] == "https://img.kookapp.cn/a.png" for s in msg.segments)


def test_parse_recalled_and_observed_system_events():
    deleted = {
        "type": 255,
        "target_id": "ch-1",
        "extra": {"type": "deleted_message", "body": {"msg_id": "dead-uuid", "channel_id": "ch-1"}},
    }
    assert parse_recalled(deleted) == ("ch-1", "dead-uuid")

    joined = {
        "type": 255,
        "target_id": "g-9",
        "extra": {"type": "self_joined_guild", "body": {"guild_id": "g-9"}},
    }
    obs = parse_observed(joined)
    assert obs is not None
    assert obs.kind == "kook.self_joined_guild"
    assert obs.guild_id == "g-9"


def test_parse_updated_message_and_batch_channel_and_cache_events():
    edited = {
        "type": 255,
        "target_id": "ch-1",
        "msg_timestamp": 1_700_000_500_000,
        "extra": {
            "type": "updated_message",
            "body": {
                "msg_id": "msg-1",
                "channel_id": "ch-1",
                "content": "修改后的消息",
                "updated_at": 1_700_000_500_000,
            },
        },
    }
    assert parse_updated_message(edited) == ("ch-1", "msg-1", "修改后的消息", 1_700_000_500)

    batch_added = {
        "type": 255,
        "target_id": "g-1",
        "extra": {
            "type": "batch_added_channel",
            "body": [
                {"id": "ch-a", "guild_id": "g-1", "name": "帖子区", "type": 4},
                {"id": "ch-b", "guild_id": "g-1", "name": "聊天区", "type": 1},
            ],
        },
    }
    obs_add = parse_observed(batch_added)
    assert obs_add is not None
    assert obs_add.kind == "kook.batch_added_channel"
    assert obs_add.guild_id == "g-1"
    assert len(obs_add.data["items"]) == 2

    batch_deleted = {
        "type": 255,
        "target_id": "g-1",
        "extra": {"type": "batch_deleted_channel", "body": [{"id": "ch-a"}, {"id": "ch-b"}]},
    }
    obs_del = parse_observed(batch_deleted)
    assert obs_del is not None
    assert obs_del.kind == "kook.batch_deleted_channel"
    assert [x["id"] for x in obs_del.data["items"]] == ["ch-a", "ch-b"]

    role_ev = {
        "type": 255,
        "target_id": "g-1",
        "extra": {"type": "updated_role", "body": {"role_id": 42, "name": "管理"}},
    }
    obs_role = parse_observed(role_ev)
    assert obs_role is not None
    assert obs_role.kind == "kook.updated_role"
    assert obs_role.guild_id == "g-1"


def test_parse_forum_thread_and_nested_posts():
    card_content = json.dumps(
        [
            {
                "type": "card",
                "theme": "invisible",
                "modules": [
                    {"type": "section", "text": {"type": "kmarkdown", "content": "这是帖子**完整正文**"}},
                    {"type": "container", "elements": [{"type": "image", "src": "https://img.kookapp.cn/p1.png"}]},
                ],
            }
        ]
    )
    # 1. /thread/list summary (content=None) falls back to preview_content
    summary = parse_forum_thread(
        {
            "id": "th-1",
            "title": "新手攻略",
            "preview_content": "先看这里",
            "content": None,
            "create_time": 1_700_000_000_000,
            "latest_active_time": 1_700_000_900_000,
            "post_count": 3,
            "user": {"id": "u-1", "username": "alice", "is_master": True, "identify_num": "1234", "online": True},
            "category": {"id": "c-1", "name": "教程"},
            "tags": [{"id": 9, "name": "精华"}],
        },
        channel_id="ch-forum",
        guild_id="g-1",
    )
    assert summary.content is None
    assert summary.content_text == "先看这里"
    assert summary.author.role == "owner"
    assert summary.author.identify_num == "1234"
    assert summary.category is not None
    assert summary.category.name == "教程"
    assert [t.name for t in summary.tags] == ["精华"]

    # 2. /thread/view full payload decodes CardMessage JSON & image segments
    detail = parse_forum_thread(
        {
            "id": "th-1",
            "post_id": "p-main",
            "title": "新手攻略",
            "preview_content": "先看这里",
            "content": card_content,
            "create_time": 1_700_000_000_000,
            "latest_active_time": 1_700_000_900_000,
            "post_count": 3,
            "collect_num": 5,
            "user": {"id": "u-1", "username": "alice"},
            "medias": [{"type": 1, "src": "https://img.kookapp.cn/m1.png", "title": "截图"}],
        },
        channel_id="ch-forum",
        guild_id="g-1",
    )
    assert "完整正文" in detail.content_text
    assert any(
        s["type"] == "image" and s["data"]["url"] == "https://img.kookapp.cn/p1.png" for s in detail.content_segments
    )
    assert detail.medias[0].src == "https://img.kookapp.cn/m1.png"

    # 3. /thread/post parses comment + nested floor replies
    reply_card = json.dumps(
        [
            {
                "type": "card",
                "theme": "invisible",
                "modules": [{"type": "section", "text": {"type": "kmarkdown", "content": "赞！"}}],
            }
        ]
    )
    post = parse_forum_post(
        {
            "id": "p-10",
            "thread_id": "th-1",
            "content": card_content,
            "create_time": 1_700_000_100_000,
            "user": {"id": "u-2", "username": "bob"},
            "replies": [
                {
                    "id": "p-11",
                    "thread_id": "th-1",
                    "belong_to_post_id": "p-10",
                    "reply_id": "p-10",
                    "content": reply_card,
                    "create_time": 1_700_000_200_000,
                    "user": {"id": "u-3", "username": "carol"},
                }
            ],
        }
    )
    assert post.post_id == "p-10"
    assert "完整正文" in post.content_text
    assert len(post.replies) == 1
    assert post.replies[0].content_text == "赞！"
    assert post.replies[0].belong_to_post_id == "p-10"


def test_parse_card_or_kmarkdown_extracts_invisible_card_edits_without_json_leak():
    invisible_card = json.dumps(
        [
            {
                "type": "card",
                "theme": "invisible",
                "size": "lg",
                "modules": [
                    {"type": "section", "text": {"type": "kmarkdown", "content": "编辑后的无边框正文"}},
                    {"type": "countdown", "mode": "second", "endTime": 1_700_000_000_000},
                    {"type": "invite", "code": "abc123"},
                ],
            }
        ]
    )
    segs = parse_card_or_kmarkdown(invisible_card)
    assert segs[0]["type"] == "card"
    plain = segs[0]["data"]["plain_text"]
    assert "编辑后的无边框正文" in plain
    assert "[倒计时]" in plain
    assert "[邀请:abc123]" in plain
    assert '"theme"' not in plain

    kmd_segs = parse_card_or_kmarkdown("普通编辑消息")
    assert kmd_segs == [{"type": "text", "data": {"text": "普通编辑消息"}}]
