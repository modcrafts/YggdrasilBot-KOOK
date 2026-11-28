import json

import pytest

from yggdrasil.kook.parser import parse_incoming
from yggdrasil.ops import agent


@pytest.mark.parametrize("channel_type", ["GROUP", "PERSON"])
def test_ops_keeps_selected_members_at_their_original_positions(channel_type: str) -> None:
    incoming = parse_incoming(
        {
            "type": 9,
            "channel_type": channel_type,
            "msg_id": "message",
            "author_id": "123",
            "content": "(met)100(met) ops 查(met)201(met)和(met)202(met)，跳过手打的 @小王",
            "extra": {"kmarkdown": {"mention_part": [{"id": uid, "username": "小王"} for uid in ("201", "202")]}},
        },
        self_id="100",
    )
    assert incoming is not None
    assert incoming.text == "ops 查@小王 和@小王 ，跳过手打的 @小王"
    instruction = incoming.text.removeprefix("ops ")
    prompt, blocks = agent._prompt(incoming, instruction, None, "opsabc")  # pyright: ignore[reportPrivateUsage]
    assert blocks == ["成员提及", "指令"]
    members, command = prompt.split("\n\n")
    assert json.loads(members.splitlines()[1]) == [
        " ops 查",
        {"user_id": "201", "name": "小王"},
        "和",
        {"user_id": "202", "name": "小王"},
        "，跳过手打的 @小王",
    ]
    assert command == f"<指令-opsabc>\n{instruction}\n</指令-opsabc>"


def test_ops_plain_names_and_group_mentions_have_no_member_id() -> None:
    incoming = parse_incoming(
        {
            "type": 9,
            "channel_type": "GROUP",
            "msg_id": "message",
            "author_id": "123",
            "content": "(met)100(met) ops 查 @小王 (met)all(met)(met)here(met)(rol)301(rol)",
        },
        self_id="100",
    )
    assert incoming is not None
    prompt, blocks = agent._prompt(incoming, "查 @小王", None, "opsabc")  # pyright: ignore[reportPrivateUsage]
    assert blocks == ["指令"]
    assert prompt == "<指令-opsabc>\n查 @小王\n</指令-opsabc>"
