"""get_post_comments reads a channel post's comment thread (messages.getReplies via
Telethon's reply_to) and can look up the personal channels of comment authors."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError, MsgIdInvalidError
from telethon.helpers import TotalList
from telethon.tl import types

from telegram_mcp import runtime
from telegram_mcp.tools import comments

DATE = datetime(2026, 11, 18, tzinfo=timezone.utc)
CHANNEL_ID = 1234567890
DISCUSSION_ID = 1987654321
POST_ID = 4242

ALICE, BOB, CAROL = 111, 222, 333
ALICE_CHANNEL_ID = 5550001
CAROL_CHANNEL_ID = 5550003


def _channel(channel_id, title="Channel", **kwargs):
    return types.Channel(
        id=channel_id, title=title, photo=types.ChatPhotoEmpty(), date=DATE, **kwargs
    )


def _comment(msg_id, from_peer):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(DISCUSSION_ID),
        date=DATE,
        message=f"comment {msg_id}",
        from_id=from_peer,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=1, reply_to_top_id=1),
    )


def _comments(*messages, total=None):
    result = TotalList(messages)
    result.total = len(messages) if total is None else total
    return result


def _full_user(personal_channel_id=None, chats=()):
    return SimpleNamespace(
        full_user=SimpleNamespace(personal_channel_id=personal_channel_id), chats=list(chats)
    )


PROFILES = {
    ALICE: _full_user(
        ALICE_CHANNEL_ID, [_channel(ALICE_CHANNEL_ID, "Alice builds", username="alice_builds")]
    ),
    BOB: _full_user(),
    CAROL: _full_user(
        CAROL_CHANNEL_ID,
        [
            _channel(
                CAROL_CHANNEL_ID,
                "Carol notes",
                usernames=[types.Username(username="carol_notes", active=True)],
            )
        ],
    ),
}


@pytest.fixture
def client(monkeypatch):
    cl = AsyncMock()
    cl.side_effect = lambda request: PROFILES[request.id]
    monkeypatch.setattr(comments, "get_client", lambda account=None: cl)
    monkeypatch.setattr(comments, "ensure_connected", AsyncMock())
    monkeypatch.setattr(
        comments, "resolve_entity", AsyncMock(return_value=_channel(CHANNEL_ID, broadcast=True))
    )
    return cl


def _requested_user_ids(cl):
    return [c.args[0].id for c in cl.await_args_list]


@pytest.mark.asyncio
async def test_comments_are_read_through_reply_to(client):
    client.get_messages.return_value = _comments(
        _comment(12, types.PeerUser(ALICE)), _comment(11, types.PeerUser(BOB)), total=37
    )

    result = json.loads(
        await comments.get_post_comments(chat_id="@some_channel", message_id=POST_ID)
    )

    assert client.get_messages.await_args.kwargs == {"limit": 50, "reply_to": POST_ID}
    assert [r["id"] for r in result["results"]] == [12, 11]
    assert [r["sender_id"] for r in result["results"]] == [ALICE, BOB]
    assert result["total"] == 37
    assert "personal_channels" not in result
    client.assert_not_awaited()  # no profile lookups unless asked


@pytest.mark.asyncio
async def test_limit_is_passed_through(client):
    client.get_messages.return_value = _comments()
    await comments.get_post_comments(chat_id="@some_channel", message_id=POST_ID, limit=5)
    assert client.get_messages.await_args.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_personal_channels_of_distinct_human_authors(client):
    client.get_messages.return_value = _comments(
        _comment(15, types.PeerUser(ALICE)),
        _comment(14, types.PeerChannel(CHANNEL_ID)),  # posted as the channel itself
        _comment(13, types.PeerUser(BOB)),
        _comment(12, types.PeerUser(ALICE)),  # same author again
        _comment(11, types.PeerUser(CAROL)),
    )

    result = json.loads(
        await comments.get_post_comments(
            chat_id="@some_channel", message_id=POST_ID, with_personal_channels=True
        )
    )

    assert _requested_user_ids(client) == [ALICE, BOB, CAROL]
    assert result["personal_channels_checked"] == 3
    assert result["personal_channels"] == {
        str(ALICE): {
            "chat_id": -1000000000000 - ALICE_CHANNEL_ID,
            "title": "Alice builds",
            "username": "alice_builds",
            "link": "https://t.me/alice_builds",
        },
        str(CAROL): {
            "chat_id": -1000000000000 - CAROL_CHANNEL_ID,
            "title": "Carol notes",
            "username": "carol_notes",
            "link": "https://t.me/carol_notes",
        },
    }
    assert "personal_channels_not_checked" not in result


@pytest.mark.asyncio
async def test_lookups_are_capped(client, monkeypatch):
    monkeypatch.setattr(comments, "PERSONAL_CHANNEL_LOOKUP_CAP", 1)
    client.get_messages.return_value = _comments(
        _comment(12, types.PeerUser(ALICE)), _comment(11, types.PeerUser(BOB))
    )

    result = json.loads(
        await comments.get_post_comments(
            chat_id="@some_channel", message_id=POST_ID, with_personal_channels=True
        )
    )

    assert _requested_user_ids(client) == [ALICE]
    assert result["personal_channels_checked"] == 1
    assert result["personal_channels_not_checked"] == 1


@pytest.mark.asyncio
async def test_flood_wait_stops_lookups_but_keeps_comments(client):
    client.get_messages.return_value = _comments(
        _comment(12, types.PeerUser(ALICE)), _comment(11, types.PeerUser(BOB))
    )

    def flood_on_bob(request):
        if request.id == BOB:
            raise FloodWaitError(request=None, capture=30)
        return PROFILES[request.id]

    client.side_effect = flood_on_bob

    result = json.loads(
        await comments.get_post_comments(
            chat_id="@some_channel", message_id=POST_ID, with_personal_channels=True
        )
    )

    assert [r["id"] for r in result["results"]] == [12, 11]
    assert list(result["personal_channels"]) == [str(ALICE)]
    assert result["personal_channels_stopped"] == "flood wait 30s"


@pytest.mark.asyncio
async def test_one_failed_profile_does_not_drop_the_rest(client):
    client.get_messages.return_value = _comments(
        _comment(12, types.PeerUser(BOB)), _comment(11, types.PeerUser(ALICE))
    )

    def bob_fails(request):
        if request.id == BOB:
            raise ValueError("Could not find the input entity")
        return PROFILES[request.id]

    client.side_effect = bob_fails

    result = json.loads(
        await comments.get_post_comments(
            chat_id="@some_channel", message_id=POST_ID, with_personal_channels=True
        )
    )

    assert list(result["personal_channels"]) == [str(ALICE)]
    assert result["personal_channels_checked"] == 1


@pytest.mark.asyncio
async def test_post_without_comment_section(client):
    client.get_messages.side_effect = MsgIdInvalidError(request=None)

    result = await comments.get_post_comments(chat_id="@some_channel", message_id=POST_ID)

    assert result == comments.NO_COMMENT_SECTION.format(
        message_id=POST_ID, chat_id="@some_channel"
    )


@pytest.mark.asyncio
async def test_non_channel_is_refused(client, monkeypatch):
    monkeypatch.setattr(
        comments,
        "resolve_entity",
        AsyncMock(return_value=types.User(id=ALICE, first_name="Alice")),
    )

    result = await comments.get_post_comments(chat_id="alice", message_id=POST_ID)

    assert result == comments.NOT_A_CHANNEL.format(chat_id="alice")
    client.get_messages.assert_not_awaited()


def test_personal_channel_missing_from_response_falls_back_to_raw_id():
    assert comments.personal_channel_entry(_full_user(ALICE_CHANNEL_ID)) == {
        "chat_id": ALICE_CHANNEL_ID
    }


def test_no_personal_channel():
    assert comments.personal_channel_entry(_full_user()) is None


def test_registered_as_read_only_history_tool():
    registered = {tool.name: tool for tool in runtime.mcp._tool_manager.list_tools()}
    assert registered["get_post_comments"].annotations.readOnlyHint is True
    assert runtime.category_for_tool("get_post_comments") == "history"
