"""Unit tests for pylyrion player helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pylyrion.client import LyrionClient
from pylyrion.models import LyrionEndpoint
from pylyrion.player import LyrionPlayerClient
from pylyrion.session import LyrionSession
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


def _build_player_client() -> LyrionPlayerClient:
    """Create a pylyrion player client backed by a fake HTTP transport."""
    http_session = SimpleNamespace(post=MagicMock(return_value=FakeResponse({"result": {}})))
    session = LyrionSession(
        http_session=http_session,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    return LyrionPlayerClient(session)


@pytest.mark.asyncio
async def test_player_client_get_status() -> None:
    """Player status should be requested through the raw pylyrion client."""
    transport = SimpleNamespace(
        post=MagicMock(return_value=FakeResponse({"result": {"mode": "play"}}))
    )
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionPlayerClient(session)

    assert await client.get_status("player1") == {"mode": "play"}


@pytest.mark.asyncio
async def test_high_level_client_exposes_player_control() -> None:
    """The high-level client should expose player helpers through one facade."""
    transport = SimpleNamespace(
        post=MagicMock(return_value=FakeResponse({"result": {"mode": "stop"}}))
    )
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionClient(session)

    assert await client.players.get_status("player1") == {"mode": "stop"}


@pytest.mark.asyncio
async def test_player_client_transport_methods_dispatch_expected_commands() -> None:  # noqa: PLR0915
    """High-level transport helpers should dispatch LMS-native command payloads."""
    transport = SimpleNamespace(post=MagicMock(return_value=FakeResponse({"result": {"ok": True}})))
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionPlayerClient(session)

    await client.play("player1")
    await client.pause("player1")
    await client.stop("player1")
    await client.set_power("player1", True)
    await client.unsync("player1")
    await client.sync_to("player1", "leader1")
    await client.get_queue_status("player1", offset=0, limit=33)
    await client.set_queue_index("player1", 4)
    await client.next_track("player1")
    await client.previous_track("player1")
    await client.set_repeat_mode("player1", 2)
    await client.set_shuffle_mode("player1", 1)
    await client.clear_queue("player1")
    await client.set_volume("player1", 101)
    await client.set_muted("player1", True)
    await client.seek("player1", -8)
    await client.set_sync_volume("player1", False)
    await client.add_track_id("player1", "t-42", command="load")
    await client.move_queue_item("player1", 6, 1)
    await client.delete_queue_item("player1", 3)
    await client.play_url("player1", "http://example/play")
    await client.add_url("player1", "http://example/add")
    await client.add_url_to_queue(
        "player1",
        "http://example/stream",
        title="Title",
        artist="Artist",
        album="Album",
    )

    calls = [call.kwargs["json"]["params"] for call in transport.post.call_args_list]
    assert calls[0] == ["player1", ["play"]]
    assert calls[1] == ["player1", ["pause", 1]]
    assert calls[2] == ["player1", ["stop"]]
    assert calls[3] == ["player1", ["power", 1]]
    assert calls[4] == ["player1", ["sync", "-"]]
    assert calls[5] == ["player1", ["sync", "leader1"]]
    assert calls[6] == ["player1", ["status", 0, 33]]
    assert calls[7] == ["player1", ["playlist", "index", 4]]
    assert calls[8] == ["player1", ["playlist", "index", "+1"]]
    assert calls[9] == ["player1", ["playlist", "index", "-1"]]
    assert calls[10] == ["player1", ["playlist", "repeat", 2]]
    assert calls[11] == ["player1", ["playlist", "shuffle", 1]]
    assert calls[12] == ["player1", ["playlist", "clear"]]
    assert calls[13] == ["player1", ["mixer", "volume", 100]]
    assert calls[14] == ["player1", ["mixer", "muting", 1]]
    assert calls[15] == ["player1", ["time", 0]]
    assert calls[16] == ["player1", ["playerpref", "syncVolume", 0]]
    assert calls[17] == ["player1", ["playlistcontrol", "cmd:load", "track_id:t-42"]]
    assert calls[18] == ["player1", ["playlist", "move", 6, 1]]
    assert calls[19] == ["player1", ["playlist", "delete", 3]]
    assert calls[20] == ["player1", ["playlist", "play", "http://example/play"]]
    assert calls[21] == ["player1", ["playlist", "add", "http://example/add"]]
    assert calls[22] == [
        "player1",
        [
            "playlistcontrol",
            "cmd:add",
            "url:http://example/stream",
            "title:Title",
            "artist:Artist",
            "album:Album",
        ],
    ]


@pytest.mark.asyncio
async def test_player_client_add_url_to_queue_falls_back_to_playlist_add() -> None:
    """URL adds should fall back to playlist add when LMS rejects metadata payload."""
    transport = SimpleNamespace(
        post=MagicMock(
            side_effect=[
                FakeResponse(
                    {
                        "error": {
                            "code": -32603,
                            "message": "unsupported metadata args",
                        },
                        "result": {},
                    }
                ),
                FakeResponse({"result": {"ok": True}}),
            ]
        )
    )
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionPlayerClient(session)

    await client.add_url_to_queue("player1", "http://example/stream")

    calls = [call.kwargs["json"]["params"] for call in transport.post.call_args_list]
    assert calls[0] == [
        "player1",
        ["playlistcontrol", "cmd:add", "url:http://example/stream"],
    ]
    assert calls[1] == ["player1", ["playlist", "add", "http://example/stream"]]
