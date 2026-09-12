# mypy: disable-error-code="method-assign,attr-defined"
"""Tests for Lyrion provider lifecycle and task wiring helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.lyrion_music import provider as lyrion_provider_mod
from music_assistant.providers.lyrion_music.constants import ACTION_RESCAN_ARTWORK
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


async def test_config_action_triggers_artwork_rescan(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Rescan action should rotate cache token and queue artwork tasks."""
    lyrion_provider._rotate_artwork_cache_token = Mock()
    lyrion_provider._trigger_artwork_backfill_tasks = Mock()

    result = await lyrion_provider.handle_config_action(ACTION_RESCAN_ARTWORK)

    lyrion_provider._rotate_artwork_cache_token.assert_called_once()
    lyrion_provider._trigger_artwork_backfill_tasks.assert_called_once()
    assert result is not None


async def test_config_action_unknown_delegates_to_super(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Unknown action should delegate to base class and surface its error."""
    with pytest.raises(ActionUnavailable, match="Unknown action"):
        await lyrion_provider.handle_config_action("unknown-action")


async def test_loaded_in_mass_registers_subscription_and_tasks(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Provider load should subscribe to sync event and register scheduled tasks."""
    unsubscribe = Mock()
    lyrion_provider.mass.subscribe = Mock(return_value=unsubscribe)
    lyrion_provider.mass.tasks.register_scheduled_task = Mock()
    lyrion_provider.mass.tasks.set_task_enabled = Mock()

    with patch.object(MusicProvider, "loaded_in_mass", new=AsyncMock()):
        await lyrion_provider.loaded_in_mass()

    lyrion_provider.mass.subscribe.assert_called_once()
    assert lyrion_provider._unsubscribe_music_sync_completed is unsubscribe
    assert lyrion_provider.mass.tasks.register_scheduled_task.call_count == 2
    assert lyrion_provider.mass.tasks.set_task_enabled.call_count == 2


async def test_unload_unregisters_tasks_and_unsubscribes(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Provider unload should unsubscribe and unregister artwork tasks."""
    unsubscribe = Mock()
    lyrion_provider._unsubscribe_music_sync_completed = unsubscribe
    lyrion_provider.mass.tasks.unregister_scheduled_task = Mock()

    with patch.object(MusicProvider, "unload", new=AsyncMock()):
        await lyrion_provider.unload(is_removed=True)

    unsubscribe.assert_called_once()
    assert lyrion_provider.mass.tasks.unregister_scheduled_task.call_count == 2


def test_music_sync_completed_event_respects_unloading(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Sync-completed event should no-op while unloading and trigger otherwise."""
    lyrion_provider._trigger_artwork_backfill_tasks = Mock()

    lyrion_provider.unloading = True
    lyrion_provider._on_music_sync_completed(Mock())
    lyrion_provider._trigger_artwork_backfill_tasks.assert_not_called()

    lyrion_provider.unloading = False
    lyrion_provider._on_music_sync_completed(Mock())
    lyrion_provider._trigger_artwork_backfill_tasks.assert_called_once()


def test_trigger_artwork_backfill_swallow_invalid_data(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """InvalidDataError should not block scheduling attempts for remaining tasks."""
    lyrion_provider.mass.tasks.run_task = Mock(side_effect=[InvalidDataError("boom"), None])

    lyrion_provider._trigger_artwork_backfill_tasks()

    assert lyrion_provider.mass.tasks.run_task.call_count == 2


def test_rotate_artwork_cache_token_persists_immediately(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Cache token rotation should update setup_data with immediate persistence."""
    lyrion_provider._update_setup_data = Mock()

    lyrion_provider._rotate_artwork_cache_token()

    assert lyrion_provider._update_setup_data.call_count == 1
    args = lyrion_provider._update_setup_data.call_args
    assert args.kwargs["immediate"] is True


def test_artwork_task_id_properties(lyrion_provider: LyrionMusicProvider) -> None:
    """Artwork task ids should be stable and based on provider instance id."""
    assert lyrion_provider._album_artwork_task_id.endswith("_album_artwork_sync")
    assert lyrion_provider._artist_artwork_task_id.endswith("_artist_artwork_sync")


def test_supported_media_types_are_exposed(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Provider should expose all media types implemented by browse/search/get methods."""
    assert lyrion_provider.supported_media_types == {
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.TRACK,
        MediaType.PLAYLIST,
    }


async def test_get_config_entries_exposes_rescan_action(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Config entries should expose only the artwork rescan action entry."""
    entries = await lyrion_provider.get_config_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == ACTION_RESCAN_ARTWORK
    assert entry.type == ConfigEntryType.ACTION
    assert entry.action == ACTION_RESCAN_ARTWORK


async def test_sync_method_wrappers_delegate_to_sync_module(
    lyrion_provider: LyrionMusicProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-level sync methods should delegate to sync helper module methods."""
    monkeypatch.setattr(
        lyrion_provider_mod.sync, "sync_library_artists", AsyncMock(return_value={1})
    )
    monkeypatch.setattr(
        lyrion_provider_mod.sync, "sync_library_albums", AsyncMock(return_value={2})
    )
    monkeypatch.setattr(
        lyrion_provider_mod.sync, "sync_library_tracks", AsyncMock(return_value={3})
    )
    monkeypatch.setattr(lyrion_provider_mod.sync, "sync_artist_artwork_from_library", AsyncMock())
    monkeypatch.setattr(lyrion_provider_mod.sync, "sync_album_artwork_from_library", AsyncMock())

    assert await lyrion_provider._sync_library_artists() == {1}
    assert await lyrion_provider._sync_library_albums() == {2}
    assert await lyrion_provider._sync_library_tracks() == {3}

    await lyrion_provider._sync_artist_artwork_from_library()
    await lyrion_provider._sync_album_artwork_from_library()

    lyrion_provider_mod.sync.sync_library_artists.assert_awaited_once_with(lyrion_provider)
    lyrion_provider_mod.sync.sync_library_albums.assert_awaited_once_with(lyrion_provider)
    lyrion_provider_mod.sync.sync_library_tracks.assert_awaited_once_with(lyrion_provider)
    lyrion_provider_mod.sync.sync_artist_artwork_from_library.assert_awaited_once_with(
        lyrion_provider
    )
    lyrion_provider_mod.sync.sync_album_artwork_from_library.assert_awaited_once_with(
        lyrion_provider
    )
