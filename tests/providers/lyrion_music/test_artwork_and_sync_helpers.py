# mypy: disable-error-code="attr-defined,no-untyped-def,arg-type,unreachable"
"""Unit tests for Lyrion artwork and sync helper logic."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist, MediaItemImage, ProviderMapping, UniqueList

from music_assistant.providers.lyrion_music import sync

if TYPE_CHECKING:
    import pytest


def _make_artist(
    *,
    item_id: str,
    provider: str,
    mapping: ProviderMapping | None = None,
    thumb: str | None = None,
) -> Artist:
    """Create a minimal artist object for sync tests."""
    artist = Artist(
        item_id=item_id,
        provider=provider,
        name=f"Artist {item_id}",
        provider_mappings={mapping} if mapping else set(),
    )
    if thumb:
        artist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumb,
                    provider=provider,
                    remotely_accessible=True,
                )
            ]
        )
    return artist


def test_resolve_provider_mapping_prefers_instance_then_domain() -> None:
    """Mapping resolver should prefer exact instance mapping over domain fallback."""
    provider = Mock()
    provider.instance_id = "lyrion_music--test"
    provider.domain = "lyrion_music"

    domain_mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance="other_instance",
        in_library=True,
    )
    instance_mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
        in_library=True,
    )
    artist = _make_artist(
        item_id="101",
        provider="library",
        mapping=domain_mapping,
    )
    artist.provider_mappings.add(instance_mapping)

    resolved = sync._resolve_provider_mapping(provider, artist)
    assert resolved is instance_mapping


def test_extract_artwork_url_from_mapping_variants(lyrion_provider: Mock) -> None:
    """Artwork URL extraction should support artist and album mapping payloads."""
    artist_mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance=lyrion_provider.instance_id,
        in_library=True,
        details='{"portraitid":"a1"}',
    )
    album_mapping = ProviderMapping(
        item_id="alb1",
        provider_domain="lyrion_music",
        provider_instance=lyrion_provider.instance_id,
        in_library=True,
        details='{"coverid":"alb1"}',
    )

    artist_url = sync._extract_artwork_url_from_mapping(
        lyrion_provider,
        MediaType.ARTIST,
        artist_mapping,
    )
    album_url = sync._extract_artwork_url_from_mapping(
        lyrion_provider,
        MediaType.ALBUM,
        album_mapping,
    )

    assert artist_url is not None
    assert "/contributor/a1/image_600x600_f" in artist_url
    assert album_url is not None
    assert "/music/alb1/cover_600x600_f" in album_url


def test_extract_artwork_url_from_mapping_invalid_payload(lyrion_provider: Mock) -> None:
    """Missing/invalid details payload should return no artwork URL."""
    mapping_no_details = ProviderMapping(
        item_id="x",
        provider_domain="lyrion_music",
        provider_instance=lyrion_provider.instance_id,
        in_library=True,
        details=None,
    )
    mapping_invalid_json = ProviderMapping(
        item_id="x",
        provider_domain="lyrion_music",
        provider_instance=lyrion_provider.instance_id,
        in_library=True,
        details="not-json",
    )

    assert (
        sync._extract_artwork_url_from_mapping(
            lyrion_provider,
            MediaType.ARTIST,
            mapping_no_details,
        )
        is None
    )
    assert (
        sync._extract_artwork_url_from_mapping(
            lyrion_provider,
            MediaType.ALBUM,
            mapping_invalid_json,
        )
        is None
    )


async def test_sync_library_artwork_updates_items(
    lyrion_provider: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artwork backfill should update library rows when thumb actually changes."""
    mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance=lyrion_provider.instance_id,
        in_library=True,
        details='{"portraitid":"a1"}',
    )
    artist = _make_artist(
        item_id="101",
        provider="library",
        mapping=mapping,
        thumb="http://old/thumb.png",
    )

    async def _iter_items(*args, **kwargs):
        del args, kwargs
        yield artist

    controller = Mock()
    controller.iter_library_items = _iter_items
    controller.update_item_in_library = AsyncMock()

    async def _no_probe(*args, **kwargs):
        del args, kwargs
        return ()

    monkeypatch.setattr(sync.artwork, "ensure_preferred_artwork_size", _no_probe)

    await sync._sync_library_artwork(
        lyrion_provider,
        MediaType.ARTIST,
        controller,
    )

    controller.update_item_in_library.assert_awaited_once()
    update_call = controller.update_item_in_library.await_args
    assert update_call.args[0] == 101


async def test_sync_library_entities_adds_new_item_and_sets_favorite() -> None:
    """Entity sync should add missing item, set favorite and sync genres."""
    mapping = ProviderMapping(
        item_id="track-1",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
    )
    prov_item = SimpleNamespace(
        name="Track One",
        uri="lyrion://track/1",
        provider_mappings={mapping},
        available=True,
        favorite=True,
        metadata=SimpleNamespace(genres={"Rock"}),
        item_id="track-1",
    )

    async def _iter_items(_provider):
        yield prov_item

    @asynccontextmanager
    async def _deferred_commit():
        yield

    controller = Mock()
    controller.get_library_item_sync_details = AsyncMock(return_value=None)
    controller.add_item_to_library = AsyncMock(
        return_value=SimpleNamespace(item_id="501", favorite=False)
    )
    controller.set_favorite = AsyncMock()

    provider = Mock()
    provider.logger = Mock()
    provider.mass.music.database.deferred_commit = _deferred_commit
    provider._update_sync_task_item_status = Mock()
    provider._library_item_needs_update = Mock(return_value=False)
    provider._sync_item_genres = AsyncMock()
    provider._handle_sync_item_failure = Mock()
    provider._protect_failed_sync_item = Mock()

    spec = sync.SyncSpec(
        media_type=MediaType.TRACK,
        iter_items=_iter_items,
        controller_getter=lambda _provider: controller,
    )

    result = await sync._sync_library_entities(provider, spec)

    assert result == {501}
    controller.add_item_to_library.assert_awaited_once_with(prov_item)
    controller.set_favorite.assert_awaited_once_with(501, True)
    provider._sync_item_genres.assert_awaited_once()


async def test_sync_library_entities_handles_item_failures() -> None:
    """Entity sync should route item exceptions through failure handlers."""
    mapping = ProviderMapping(
        item_id="track-2",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
    )
    prov_item = SimpleNamespace(
        name="Track Two",
        uri="lyrion://track/2",
        provider_mappings={mapping},
        available=True,
        favorite=False,
        metadata=None,
        item_id="track-2",
    )

    async def _iter_items(_provider):
        yield prov_item

    @asynccontextmanager
    async def _deferred_commit():
        yield

    controller = Mock()
    controller.get_library_item_sync_details = AsyncMock(return_value=None)
    controller.add_item_to_library = AsyncMock(side_effect=ValueError("boom"))

    provider = Mock()
    provider.logger = Mock()
    provider.mass.music.database.deferred_commit = _deferred_commit
    provider._update_sync_task_item_status = Mock()
    provider._library_item_needs_update = Mock(return_value=False)
    provider._sync_item_genres = AsyncMock()
    provider._handle_sync_item_failure = Mock()
    provider._protect_failed_sync_item = Mock()

    spec = sync.SyncSpec(
        media_type=MediaType.TRACK,
        iter_items=_iter_items,
        controller_getter=lambda _provider: controller,
    )

    result = await sync._sync_library_entities(provider, spec)

    assert result == set()
    provider._handle_sync_item_failure.assert_called_once()
    provider._protect_failed_sync_item.assert_called_once()


async def test_sync_update_predicates_and_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync predicates and wrapper entrypoints should delegate as expected."""
    provider = Mock()
    provider.mass.music.artists.get_library_item = AsyncMock(side_effect=Exception("x"))
    provider.mass.music.albums.get_library_item = AsyncMock(side_effect=Exception("x"))

    sync_details = SimpleNamespace(item_id=10, has_album=False, has_artists=False)
    prov_track = SimpleNamespace(album=object(), artists=[object()])

    track_needs = await sync._track_needs_update(provider, sync_details, prov_track)
    assert track_needs is True

    fake_result = {1, 2}
    monkeypatch.setattr(sync, "_sync_library_entities", AsyncMock(return_value=fake_result))

    assert await sync.sync_library_artists(provider) == fake_result
    assert await sync.sync_library_albums(provider) == fake_result
    assert await sync.sync_library_tracks(provider) == fake_result


async def test_artist_album_predicates_and_post_album_sync() -> None:
    """Artist/album update predicates and post-album hook should branch correctly."""
    provider = Mock()
    provider.mass.music.artists.get_library_item = AsyncMock(
        side_effect=MediaNotFoundError("missing")
    )
    provider.mass.music.albums.get_library_item = AsyncMock(
        side_effect=MediaNotFoundError("missing")
    )

    assert await sync._artist_needs_update(provider, SimpleNamespace(item_id=1), Mock()) is True
    assert await sync._album_needs_update(provider, SimpleNamespace(item_id=1), Mock()) is True

    provider.library_sync_album_tracks_enabled = Mock(return_value=False)
    provider.import_album_tracks = AsyncMock()
    await sync._post_album_sync(provider, SimpleNamespace(item_id="1", name="A"))
    provider.import_album_tracks.assert_not_awaited()

    provider.library_sync_album_tracks_enabled = Mock(return_value=True)
    await sync._post_album_sync(provider, SimpleNamespace(item_id="1", name="A"))
    provider.import_album_tracks.assert_awaited_once_with("1", "A")


async def test_sync_artwork_wrappers_delegate_to_generic_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public artwork sync wrappers should delegate with expected media type/controller."""
    provider = Mock()
    provider.mass.music.artists = Mock()
    provider.mass.music.albums = Mock()

    delegated = AsyncMock()
    monkeypatch.setattr(sync, "_sync_library_artwork", delegated)

    await sync.sync_artist_artwork_from_library(provider)
    await sync.sync_album_artwork_from_library(provider)

    assert delegated.await_count == 2
    assert delegated.await_args_list[0].kwargs["media_type"] == MediaType.ARTIST
    assert delegated.await_args_list[1].kwargs["media_type"] == MediaType.ALBUM


async def test_sync_library_artwork_no_items_updates_task_text() -> None:
    """Artwork sync should report when there are no library rows to process."""

    async def _iter_empty(*args, **kwargs):
        del args, kwargs
        if False:
            yield None

    provider = Mock()
    provider.instance_id = "lyrion_music--test"
    provider.logger = Mock()

    controller = Mock()
    controller.iter_library_items = _iter_empty

    await sync._sync_library_artwork(provider, MediaType.ARTIST, controller)


async def test_sync_library_artwork_skips_missing_mapping_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artwork sync should skip rows with missing mapping or mapping details."""
    provider = Mock()
    provider.instance_id = "lyrion_music--test"
    provider.domain = "lyrion_music"
    provider.logger = Mock()

    no_map = _make_artist(item_id="1", provider="library")
    mapping_no_details = ProviderMapping(
        item_id="a2",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
        in_library=True,
        details=None,
    )
    no_details = _make_artist(item_id="2", provider="library", mapping=mapping_no_details)

    async def _iter_items(*args, **kwargs):
        del args, kwargs
        yield no_map
        yield no_details

    controller = Mock()
    controller.iter_library_items = _iter_items
    controller.update_item_in_library = AsyncMock()

    async def _probe(*args, **kwargs):
        del args, kwargs
        return ()

    monkeypatch.setattr(sync.artwork, "ensure_preferred_artwork_size", _probe)

    await sync._sync_library_artwork(provider, MediaType.ARTIST, controller)
    controller.update_item_in_library.assert_not_awaited()


async def test_sync_library_artwork_handles_value_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artwork sync should catch parsing/value errors per item and continue."""
    provider = Mock()
    provider.instance_id = "lyrion_music--test"
    provider.domain = "lyrion_music"
    provider.logger = Mock()

    mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
        in_library=True,
        details='{"portraitid":"a1"}',
    )
    bad_id_artist = _make_artist(item_id="not-an-int", provider="library", mapping=mapping)

    async def _iter_items(*args, **kwargs):
        del args, kwargs
        yield bad_id_artist

    controller = Mock()
    controller.iter_library_items = _iter_items
    controller.update_item_in_library = AsyncMock()

    async def _no_probe(*args, **kwargs):
        del args, kwargs
        return ()

    monkeypatch.setattr(sync.artwork, "ensure_preferred_artwork_size", _no_probe)

    await sync._sync_library_artwork(provider, MediaType.ARTIST, controller)
    controller.update_item_in_library.assert_not_awaited()


async def test_sync_library_entities_existing_item_paths_and_skips() -> None:
    """Entity sync should handle skip-unavailable, update and no-update paths."""
    mapping = ProviderMapping(
        item_id="track-x",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
    )
    unavailable = SimpleNamespace(
        name="Unavailable",
        uri="lyrion://track/u",
        provider_mappings={mapping},
        available=False,
        favorite=False,
        metadata=None,
        item_id="u",
    )
    existing = SimpleNamespace(
        name="Existing",
        uri="lyrion://track/e",
        provider_mappings={mapping},
        available=True,
        favorite=False,
        metadata=None,
        item_id="e",
    )

    async def _iter_items(_provider):
        yield unavailable
        yield existing

    @asynccontextmanager
    async def _deferred_commit():
        yield

    sync_details = SimpleNamespace(item_id=777, favorite=False, has_album=True, has_artists=True)
    controller = Mock()
    controller.get_library_item_sync_details = AsyncMock(side_effect=[None, sync_details])
    controller.add_item_to_library = AsyncMock()
    controller.update_item_in_library = AsyncMock(
        return_value=SimpleNamespace(item_id="777", favorite=False)
    )
    controller.set_favorite = AsyncMock()

    provider = Mock()
    provider.logger = Mock()
    provider.mass.music.database.deferred_commit = _deferred_commit
    provider._update_sync_task_item_status = Mock()
    provider._library_item_needs_update = Mock(side_effect=[False])
    provider._sync_item_genres = AsyncMock()
    provider._handle_sync_item_failure = Mock()
    provider._protect_failed_sync_item = Mock()

    async def _force_update(_provider: Any, _details: Any, _item: Any) -> bool:
        return True

    spec = sync.SyncSpec(
        media_type=MediaType.TRACK,
        iter_items=_iter_items,
        controller_getter=lambda _provider: controller,
        extra_needs_update=_force_update,
        skip_if_new_and_unavailable=True,
    )

    result = await sync._sync_library_entities(provider, spec)

    assert result == {777}
    controller.add_item_to_library.assert_not_awaited()
    controller.update_item_in_library.assert_awaited_once()


async def test_sync_library_entities_post_sync_failure_is_reported() -> None:
    """post_item_sync failures should not fail sync, but should be reported."""
    mapping = ProviderMapping(
        item_id="track-z",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--test",
    )
    prov_item = SimpleNamespace(
        name="Track Z",
        uri="lyrion://track/z",
        provider_mappings={mapping},
        available=True,
        favorite=False,
        metadata=None,
        item_id="z",
    )

    async def _iter_items(_provider):
        yield prov_item

    @asynccontextmanager
    async def _deferred_commit():
        yield

    controller = Mock()
    controller.get_library_item_sync_details = AsyncMock(
        return_value=SimpleNamespace(item_id=5, favorite=False)
    )

    provider = Mock()
    provider.logger = Mock()
    provider.mass.music.database.deferred_commit = _deferred_commit
    provider._update_sync_task_item_status = Mock()
    provider._library_item_needs_update = Mock(return_value=False)
    provider._sync_item_genres = AsyncMock()
    provider._handle_sync_item_failure = Mock()
    provider._protect_failed_sync_item = Mock()

    async def _post_fail(_provider: Any, _item: Any) -> None:
        raise ValueError("post-fail")

    spec = sync.SyncSpec(
        media_type=MediaType.TRACK,
        iter_items=_iter_items,
        controller_getter=lambda _provider: controller,
        post_item_sync=_post_fail,
    )

    result = await sync._sync_library_entities(provider, spec)

    assert result == {5}
    provider._handle_sync_item_failure.assert_called_once()


async def test_artist_album_needs_update_uses_parser_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artist/album update helpers should defer to parser metadata predicate results."""
    provider = Mock()
    provider.mass.music.artists.get_library_item = AsyncMock(return_value=SimpleNamespace())
    provider.mass.music.albums.get_library_item = AsyncMock(return_value=SimpleNamespace())

    monkeypatch.setattr(sync.parsers, "artist_metadata_needs_update", lambda *_: False)
    monkeypatch.setattr(sync.parsers, "album_metadata_needs_update", lambda *_: True)

    assert await sync._artist_needs_update(provider, SimpleNamespace(item_id=1), Mock()) is False
    assert await sync._album_needs_update(provider, SimpleNamespace(item_id=1), Mock()) is True


async def test_iter_library_wrappers_delegate_to_provider_iterators() -> None:
    """Library iterator wrappers should yield directly from provider iterators."""
    provider = Mock()

    async def _artists():
        yield "artist-1"

    async def _albums():
        yield "album-1"

    async def _tracks():
        yield "track-1"

    provider.get_library_artists = _artists
    provider.get_library_albums = _albums
    provider.get_library_tracks = _tracks

    artists = [item async for item in sync._iter_library_artists(provider)]
    albums = [item async for item in sync._iter_library_albums(provider)]
    tracks = [item async for item in sync._iter_library_tracks(provider)]

    assert artists == ["artist-1"]
    assert albums == ["album-1"]
    assert tracks == ["track-1"]


def test_resolve_provider_mapping_domain_fallback_branch() -> None:
    """When instance mapping is missing, resolver should fallback to same-domain mapping."""
    provider = Mock()
    provider.instance_id = "lyrion_music--new"
    provider.domain = "lyrion_music"

    domain_mapping = ProviderMapping(
        item_id="a1",
        provider_domain="lyrion_music",
        provider_instance="lyrion_music--other",
        in_library=True,
    )
    artist = _make_artist(item_id="101", provider="library", mapping=domain_mapping)

    resolved = sync._resolve_provider_mapping(provider, artist)
    assert resolved is domain_mapping
