"""Unit tests for Lyrion artwork helper functions."""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import Mock

import pytest
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, MediaItemImage, UniqueList

from music_assistant.providers.lyrion_music import artwork, parsers


class _Response:
    """Simple async response context manager for image helper tests."""

    def __init__(self, *, status: int, content_type: str, data: bytes) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._data = data

    async def read(self) -> bytes:
        return self._data

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _provider(
    *, host: str | None = "127.0.0.1", port: int | None = 9000, token: str | None = None
) -> Any:
    provider = Mock()
    provider._get_configured_host = Mock(return_value=host)
    provider._get_configured_port = Mock(return_value=port)
    provider.get_setup_value = Mock(return_value=token)
    provider.mass.http_session.get = Mock()
    return provider


def test_artwork_url_extractors_and_cache_buster() -> None:
    """Extractor helpers should build expected URLs and append cache busters."""
    provider = _provider(token="abc")

    from_relative_artwork = artwork.extract_artwork_url(
        provider,
        {"artwork": "/music/42/cover_600x600_f"},
    )
    assert from_relative_artwork == (
        "http://127.0.0.1:9000/music/42/cover_600x600_f?ma_lms_art=abc"
    )

    from_absolute_artwork = artwork.extract_artwork_url(
        provider,
        {"artwork_url": "https://img.example.invalid/x.png"},
    )
    assert from_absolute_artwork == "https://img.example.invalid/x.png?ma_lms_art=abc"

    album_url = artwork.extract_artwork_url(provider, {"coverid": "42"})
    assert album_url == "http://127.0.0.1:9000/music/42/cover_600x600_f?ma_lms_art=abc"

    artist_url = artwork.extract_artist_artwork_url(provider, {"portraitid": "a1"})
    assert artist_url == "http://127.0.0.1:9000/contributor/a1/image_600x600_f?ma_lms_art=abc"

    artist_from_absolute_artwork = artwork.extract_artist_artwork_url(
        provider,
        {"artwork_url": "https://img.example.invalid/artist.png"},
    )
    assert artist_from_absolute_artwork == "https://img.example.invalid/artist.png?ma_lms_art=abc"

    artist_from_generic_artwork = artwork.extract_artist_artwork_url(
        provider,
        {"icon": "/contributor/7/image?foo=bar"},
    )
    assert artist_from_generic_artwork == (
        "http://127.0.0.1:9000/contributor/7/image_600x600_f?foo=bar&ma_lms_art=abc"
    )


def test_artist_path_normalization_and_fallback_id_lookup() -> None:
    """Artist path normalization and fallback id-based URLs should work."""
    provider = _provider(token=None)

    normalized = artwork.normalize_artist_artwork_path("/contributor/7/image?foo=bar")
    assert normalized == "/contributor/7/image_600x600_f?foo=bar"

    normalized_fallback = artwork.normalize_artist_artwork_path("/images/artist/7.png")
    assert normalized_fallback == "/images/artist/7.png"

    fallback = artwork.extract_artist_artwork_url(provider, {"id": "a99"})
    assert fallback == "http://127.0.0.1:9000/imageproxy/mai/_artist/a99/image_600x600_f"


def test_to_lms_absolute_url_requires_host_and_port() -> None:
    """Absolute URL builder should reject missing host or port."""
    with pytest.raises(ProviderUnavailableError):
        artwork.to_lms_absolute_url(_provider(host=None), "/x")
    with pytest.raises(ProviderUnavailableError):
        artwork.to_lms_absolute_url(_provider(port=None), "/x")


async def test_fetch_remote_image_if_ok_variants() -> None:
    """Image fetch helper should accept only successful image responses."""
    provider = _provider()

    provider.mass.http_session.get.return_value = _Response(
        status=200,
        content_type="image/png",
        data=b"img",
    )
    assert await artwork.fetch_remote_image_if_ok(provider, "http://x") == b"img"

    provider.mass.http_session.get.return_value = _Response(
        status=200,
        content_type="text/plain",
        data=b"nope",
    )
    assert await artwork.fetch_remote_image_if_ok(provider, "http://x") is None

    provider.mass.http_session.get.return_value = _Response(
        status=404,
        content_type="image/png",
        data=b"",
    )
    assert await artwork.fetch_remote_image_if_ok(provider, "http://x") is None


async def test_probe_remote_image_and_set_thumb_path(lyrion_provider: Any) -> None:
    """Probe helper and thumbnail setter/getter should handle add/update/remove."""
    provider = _provider()
    provider.mass.http_session.get.return_value = _Response(
        status=200,
        content_type="image/jpeg",
        data=b"img",
    )
    assert await artwork.probe_remote_image(provider, "http://x") is True

    provider.mass.http_session.get.return_value = _Response(
        status=500,
        content_type="image/jpeg",
        data=b"img",
    )
    assert await artwork.probe_remote_image(provider, "http://x") is False

    artist = Artist(item_id="1", provider="lyrion", name="A", provider_mappings=set())
    assert artwork.get_thumb_path(artist) is None

    artwork.set_thumb_path(artist, "http://new")
    assert artwork.get_thumb_path(artist) == "http://new"

    artwork.set_thumb_path(artist, "http://new2")
    assert artwork.get_thumb_path(artist) == "http://new2"

    artwork.set_thumb_path(artist, None)
    assert artwork.get_thumb_path(artist) is None

    album = Album(item_id="2", provider="lyrion", name="B", provider_mappings=set())
    artwork.set_thumb_path(album, "http://alb")
    assert artwork.get_thumb_path(album) == "http://alb"

    # keep fixture in use to ensure compatibility with real provider model instances
    assert lyrion_provider.instance_id == "lyrion_music--test"


async def test_resolve_image_branching(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_image should handle passthrough, success and fallback failure branches."""
    provider = _provider()

    result = await artwork.resolve_image(provider, "http://x/image_300x300_f")
    assert result == "http://x/image_300x300_f"

    async def _fetch_ok(*args, **kwargs):
        del args, kwargs
        return b"img"

    monkeypatch.setattr(artwork, "fetch_remote_image_if_ok", _fetch_ok)
    result = await artwork.resolve_image(provider, "http://x/image_600x600_f")
    assert result == b"img"

    fetch_calls: list[str] = []

    async def _fetch_none(_provider: Any, path: str) -> bytes | None:
        fetch_calls.append(path)
        return None

    monkeypatch.setattr(artwork, "fetch_remote_image_if_ok", _fetch_none)
    original = "http://x/image_600x600_f"
    result = await artwork.resolve_image(provider, original)
    assert result == original
    assert fetch_calls == [original, "http://x/image_300x300_f"]


async def test_build_artist_and_album_delegate_to_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_artist/build_album should delegate to parser helpers."""
    provider = _provider()
    raw_artist = {"id": "a1", "artist": "A"}
    raw_album = {"id": "al1", "album": "AL"}

    parsed_artist = Artist(item_id="a1", provider="lyrion", name="A", provider_mappings=set())
    parsed_album = Album(item_id="al1", provider="lyrion", name="AL", provider_mappings=set())

    monkeypatch.setattr(parsers, "parse_artist", lambda _p, _r: parsed_artist)
    monkeypatch.setattr(parsers, "parse_album", lambda _p, _r: parsed_album)

    assert await artwork.build_artist(provider, raw_artist) is parsed_artist
    assert await artwork.build_album(provider, raw_album) is parsed_album


async def test_ensure_preferred_artwork_size_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_preferred_artwork_size should keep, fallback or remove thumbs."""
    provider = _provider()

    no_thumb_artist = Artist(item_id="1", provider="x", name="X", provider_mappings=set())
    assert await artwork.ensure_preferred_artwork_size(provider, no_thumb_artist) == ()

    with_thumb = Artist(item_id="2", provider="x", name="X", provider_mappings=set())
    with_thumb.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=artwork.ImageType.THUMB,
                path="http://img/cover_600x600_f",
                provider="x",
                remotely_accessible=True,
            )
        ]
    )

    async def _probe_true(*args, **kwargs):
        del args, kwargs
        return True

    monkeypatch.setattr(artwork, "probe_remote_image", _probe_true)
    attempted = await artwork.ensure_preferred_artwork_size(provider, with_thumb)
    assert attempted == ("http://img/cover_600x600_f",)
    assert artwork.get_thumb_path(with_thumb) == "http://img/cover_600x600_f"

    probe_calls: list[str] = []

    async def _probe_fallback(_provider: Any, url: str) -> bool:
        probe_calls.append(url)
        return url.endswith("300x300_f")

    monkeypatch.setattr(artwork, "probe_remote_image", _probe_fallback)
    attempted = await artwork.ensure_preferred_artwork_size(provider, with_thumb)
    assert attempted == (
        "http://img/cover_600x600_f",
        "http://img/cover_300x300_f",
    )
    assert artwork.get_thumb_path(with_thumb) == "http://img/cover_300x300_f"

    async def _probe_false(*args, **kwargs):
        del args, kwargs
        return False

    monkeypatch.setattr(artwork, "probe_remote_image", _probe_false)
    attempted = await artwork.ensure_preferred_artwork_size(provider, with_thumb)
    assert attempted == ("http://img/cover_300x300_f",)
    assert artwork.get_thumb_path(with_thumb) is None


async def test_image_helpers_handle_transport_errors() -> None:
    """Remote image helpers should gracefully handle timeout/network errors."""
    provider = _provider()
    provider.mass.http_session.get = Mock(side_effect=TimeoutError)

    assert await artwork.fetch_remote_image_if_ok(provider, "http://x") is None
    assert await artwork.probe_remote_image(provider, "http://x") is False
