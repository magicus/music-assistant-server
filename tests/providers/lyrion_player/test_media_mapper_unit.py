"""Unit tests for MA<->LMS queue mapping helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.lyrion_player.media_mapper import LmsQueueEntry, LyrionMediaMapper


def _provider_for_endpoint(host: object, port: object, instance_id: str = "prov") -> Any:
    """Build minimal provider-like object exposing endpoint setup values."""
    values = {"lms_host": host, "port": port}
    provider = SimpleNamespace(instance_id=instance_id, domain="lyrion_music")
    provider.get_setup_value = lambda key, default=None: values.get(key, default)
    return provider


def _build_mapper() -> tuple[LyrionMediaMapper, Any, Any]:
    """Create mapper with controllable player/provider/mass stubs."""
    player_provider = _provider_for_endpoint("127.0.0.1", 9000, instance_id="lyrion_player")
    mass = SimpleNamespace(providers=[], music=SimpleNamespace(get_item_by_uri=AsyncMock()))
    mass.get_provider = lambda _lookup: None
    player = SimpleNamespace(provider=player_provider, mass=mass)
    mapper = LyrionMediaMapper(cast("Any", player))
    return mapper, player, mass


@pytest.mark.asyncio
async def test_resolve_ma_uri_to_lms_queue_entry_handles_direct_url() -> None:
    """Direct stream URLs should pass through unchanged as LMS URL entries."""
    mapper, _player, _mass = _build_mapper()

    result = await mapper.resolve_ma_uri_to_lms_queue_entry("https://example.test/track.mp3")

    assert result == LmsQueueEntry(kind="url", value="https://example.test/track.mp3")


def test_lms_queue_entry_to_ma_uri_maps_track_id_and_stream_redirect_url() -> None:
    """Track ids should become MA track URIs, and redirect URLs should expose original ma_uri."""
    mapper, _player, _mass = _build_mapper()

    ma_uri_from_track = mapper.lms_queue_entry_to_ma_uri(
        LmsQueueEntry(kind="track_id", value="abc123"),
        "lyrion_music.instance",
    )
    assert "lyrion_music.instance" in ma_uri_from_track
    assert "abc123" in ma_uri_from_track

    redirected = (
        "http://127.0.0.1:8095/lyrion_player/get_stream_url?"
        "player_id=p1&queue_id=p1&queue_item_id=q1&ma_uri=spotify://track/xyz"
    )
    assert (
        mapper.lms_queue_entry_to_ma_uri(
            LmsQueueEntry(kind="url", value=redirected),
            "lyrion_music.instance",
        )
        == "spotify://track/xyz"
    )


def test_resolve_matching_music_provider_instance_uses_same_endpoint() -> None:
    """Only Lyrion music providers on the same host/port should match."""
    mapper, _player, mass = _build_mapper()
    same_endpoint = _provider_for_endpoint("127.0.0.1", 9000, instance_id="music.same")
    other_endpoint = _provider_for_endpoint("127.0.0.1", 9001, instance_id="music.other")
    other_endpoint.domain = "lyrion_music"
    same_endpoint.domain = "lyrion_music"
    mass.providers = [other_endpoint, same_endpoint]

    assert mapper.resolve_matching_music_provider_instance() == "music.same"


def test_extract_lms_entry_from_playlist_item_prefers_track_id_then_url_sources() -> None:
    """Playlist item extraction should support track ids, URL keys and URL-like title fallback."""
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"track_id": 77}
    ) == LmsQueueEntry(kind="track_id", value="77")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"url": "http://queue.local/a.mp3"}
    ) == LmsQueueEntry(kind="url", value="http://queue.local/a.mp3")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"title": "https://queue.local/b.mp3"}
    ) == LmsQueueEntry(kind="url", value="https://queue.local/b.mp3")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item({"title": "no-url"}) is None


@pytest.mark.asyncio
async def test_resolve_matching_lms_track_id_for_matching_provider_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Track URIs from matching Lyrion music providers should resolve to their LMS track id."""
    mapper, _player, mass = _build_mapper()
    music_provider = _provider_for_endpoint("127.0.0.1", 9000, instance_id="music.same")
    music_provider.domain = "lyrion_music"
    mass.get_provider = lambda lookup: music_provider if lookup == "music.same" else None

    async def _parse_uri(_uri: str) -> tuple[MediaType, str, str]:
        return (MediaType.TRACK, "music.same", "track-42")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_uri,
    )

    assert await mapper._resolve_matching_lms_track_id("dummy://uri") == "track-42"


@pytest.mark.asyncio
async def test_resolve_matching_lms_track_id_for_library_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library tracks should resolve via matching lyrion_music provider mappings."""
    mapper, _player, mass = _build_mapper()

    async def _parse_uri(_uri: str) -> tuple[MediaType, str, str]:
        return (MediaType.TRACK, "library", "db-id")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_uri,
    )

    class _TrackFake:
        def __init__(self) -> None:
            self.provider_mappings = [
                SimpleNamespace(
                    provider_domain="lyrion_music",
                    provider_instance="music.same",
                    item_id="lms-track-99",
                )
            ]

    monkeypatch.setattr("music_assistant.providers.lyrion_player.media_mapper.Track", _TrackFake)
    mass.music.get_item_by_uri = AsyncMock(return_value=_TrackFake())

    matching_provider = _provider_for_endpoint("127.0.0.1", 9000, instance_id="music.same")
    matching_provider.domain = "lyrion_music"
    mass.get_provider = lambda lookup: matching_provider if lookup == "music.same" else None

    assert await mapper._resolve_matching_lms_track_id("library://track/db-id") == "lms-track-99"


@pytest.mark.asyncio
async def test_resolve_matching_lms_track_id_handles_parse_and_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver should return None for parse errors, non-track types, and lookup failures."""
    mapper, _player, mass = _build_mapper()

    async def _parse_raises(_uri: str) -> tuple[MediaType, str, str]:
        raise MusicAssistantError("bad")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_raises,
    )
    assert await mapper._resolve_matching_lms_track_id("x") is None

    async def _parse_non_track(_uri: str) -> tuple[MediaType, str, str]:
        return (MediaType.ALBUM, "music.same", "id")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_non_track,
    )
    assert await mapper._resolve_matching_lms_track_id("x") is None

    async def _parse_library(_uri: str) -> tuple[MediaType, str, str]:
        return (MediaType.TRACK, "library", "id")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_library,
    )
    mass.music.get_item_by_uri = AsyncMock(side_effect=MusicAssistantError("missing"))
    assert await mapper._resolve_matching_lms_track_id("x") is None


def test_stream_redirect_uri_extraction_requires_expected_path_and_ma_uri() -> None:
    """Redirect extraction should require provider path and a non-empty ma_uri query value."""
    mapper, _player, _mass = _build_mapper()

    assert (
        mapper._extract_ma_uri_from_stream_redirect_url(
            "http://127.0.0.1:8095/other/get_stream_url?ma_uri=spotify://track/1"
        )
        is None
    )
    assert (
        mapper._extract_ma_uri_from_stream_redirect_url(
            "http://127.0.0.1:8095/lyrion_player/get_stream_url?x=1"
        )
        is None
    )
    assert (
        mapper._extract_ma_uri_from_stream_redirect_url(
            "http://127.0.0.1:8095/lyrion_player/get_stream_url?ma_uri="
        )
        is None
    )


def test_extract_lms_entry_from_playlist_item_supports_alternate_url_keys() -> None:
    """Playlist extraction should accept alternate LMS URL key names."""
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"uri": "https://example/uri.mp3"}
    ) == LmsQueueEntry(kind="url", value="https://example/uri.mp3")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"play_url": "https://example/play_url.mp3"}
    ) == LmsQueueEntry(kind="url", value="https://example/play_url.mp3")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"playurl": "https://example/playurl.mp3"}
    ) == LmsQueueEntry(kind="url", value="https://example/playurl.mp3")
    assert LyrionMediaMapper.extract_lms_entry_from_playlist_item(
        {"content_url": "https://example/content.mp3"}
    ) == LmsQueueEntry(kind="url", value="https://example/content.mp3")


def test_provider_endpoint_parser_rejects_invalid_host_and_port() -> None:
    """Endpoint parser should reject missing host, bad port and out-of-range port values."""
    assert LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint(None, 9000)) is None
    assert LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint(" ", 9000)) is None
    assert (
        LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint("127.0.0.1", None)) is None
    )
    assert (
        LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint("127.0.0.1", "bad")) is None
    )
    assert LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint("127.0.0.1", 0)) is None
    assert (
        LyrionMediaMapper._provider_lms_endpoint(_provider_for_endpoint("127.0.0.1", 70000)) is None
    )


def test_is_matching_music_provider_rejects_missing_or_wrong_domain() -> None:
    """Provider matching should reject unresolved providers and non-lyrion domains."""
    mapper, _player, mass = _build_mapper()

    mass.get_provider = lambda _lookup: None
    assert not mapper._is_matching_music_provider("missing")

    wrong = _provider_for_endpoint("127.0.0.1", 9000, instance_id="other")
    wrong.domain = "spotify"
    mass.get_provider = lambda _lookup: wrong
    assert not mapper._is_matching_music_provider("wrong")


@pytest.mark.asyncio
async def test_resolve_matching_lms_track_id_rejects_non_track_library_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver should return None when library lookup does not return a Track object."""
    mapper, _player, mass = _build_mapper()

    async def _parse_library(_uri: str) -> tuple[MediaType, str, str]:
        return (MediaType.TRACK, "library", "id")

    monkeypatch.setattr(
        "music_assistant.providers.lyrion_player.media_mapper.parse_uri",
        _parse_library,
    )

    class _NotTrack:
        provider_mappings: ClassVar[list[object]] = []

    mass.music.get_item_by_uri = AsyncMock(return_value=_NotTrack())
    assert await mapper._resolve_matching_lms_track_id("library://track/id") is None
