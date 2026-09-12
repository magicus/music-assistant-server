"""Lyrion artwork and image resolution helpers."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import MediaItemImage, UniqueList

from music_assistant.providers.lyrion.client import (
    acquire_lms_request_slot,
    build_lms_url,
    get_configured_basic_auth,
    get_configured_host,
    get_configured_port,
)
from pylyrion import artwork as pylyrion_artwork
from pylyrion.cometd.constants import RPC_TIMEOUT
from pylyrion.lyrion_constants import (
    ARTWORK_VALIDATION_CACHE_TTL,
    ARTWORK_VALIDATION_TIMEOUT,
    CONF_ARTWORK_CACHE_BUSTER,
)

from . import parsers

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist

    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


CACHE_CATEGORY_ARTWORK_PROBE = "lyrion_artwork_probe"


async def resolve_image(
    provider: LyrionMusicProvider,
    path: str,
) -> str | bytes:
    """Resolve artist artwork URLs with LMS size fallback when needed."""
    fallback_path = pylyrion_artwork.artwork_fallback_path(path)
    if fallback_path is None:
        return path

    if image_bytes := await fetch_remote_image_if_ok(provider, path):
        return image_bytes

    if fallback_path != path and (
        fallback_bytes := await fetch_remote_image_if_ok(
            provider,
            fallback_path,
        )
    ):
        return fallback_bytes
    return path


async def build_artist(
    provider: LyrionMusicProvider,
    row: Mapping[str, str],
) -> Artist:
    """Build artist model from LMS payload and validate artwork URL."""
    return parsers.parse_artist(provider, row)


async def build_album(
    provider: LyrionMusicProvider,
    row: Mapping[str, str],
) -> Album:
    """Build album model from LMS payload and validate artwork URL."""
    return parsers.parse_album(provider, row)


async def ensure_preferred_artwork_size(
    provider: LyrionMusicProvider,
    item: Artist | Album,
) -> tuple[str, ...]:
    """Resolve album or artist artwork by trying 600 first, then 300."""
    return await pylyrion_artwork.ensure_preferred_artwork_size(
        item,
        lambda url: probe_remote_image(provider, url),
        get_thumb_path,
        set_thumb_path,
    )


def extract_artwork_url(
    provider: LyrionMusicProvider,
    row: Mapping[str, str],
    fallback_id: str | None = None,
) -> str | None:
    """Extract an artwork URL from known LMS payload fields."""
    host = get_configured_host(provider)
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider, default=None)
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    token = provider.get_setup_value(CONF_ARTWORK_CACHE_BUSTER)
    return pylyrion_artwork.extract_artwork_url(
        row,
        host=host,
        port=port,
        fallback_id=fallback_id,
        cache_buster_token=token,
    )


def extract_artist_artwork_url(
    provider: LyrionMusicProvider,
    row: Mapping[str, str],
) -> str | None:
    """Extract artist artwork URL from known LMS artist fields."""
    host = get_configured_host(provider)
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider, default=None)
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    artist_id = parsers.extract_item_id(
        row,
        id_keys=("id", "artist_id", "contributor_id"),
    )
    token = provider.get_setup_value(CONF_ARTWORK_CACHE_BUSTER)
    return pylyrion_artwork.extract_artist_artwork_url(
        row,
        host=host,
        port=port,
        fallback_artist_id=artist_id,
        cache_buster_token=token,
    )


def normalize_artist_artwork_path(path: str) -> str:
    """Normalize LMS artist artwork path variants to a sized endpoint."""
    return pylyrion_artwork.normalize_artist_artwork_path(path)


def to_lms_absolute_url(provider: LyrionMusicProvider, path: str) -> str:
    """Build an absolute LMS URL from a relative path."""
    host = get_configured_host(provider)
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider, default=None)
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    return build_lms_url(host, port, path)


async def fetch_remote_image_if_ok(
    provider: LyrionMusicProvider,
    url: str,
) -> bytes | None:
    """Fetch image bytes and return None for invalid LMS image responses."""
    try:
        async with (
            acquire_lms_request_slot(),
            provider.mass.http_session.get(
                url,
                timeout=ClientTimeout(total=RPC_TIMEOUT),
                headers=get_configured_basic_auth(provider),
            ) as response,
        ):
            if response.status != 200:
                return None
            content_type = response.headers.get("Content-Type", "")
            if "image/" not in content_type.lower():
                return None
            data = await response.read()
            return data or None
    except TimeoutError, ClientError:
        return None


async def probe_remote_image(provider: LyrionMusicProvider, url: str) -> bool:
    """Check if a remote image endpoint is reachable without downloading payload bytes."""
    cache_key = f"probe::{url}"
    if (
        cached := await provider.mass.cache.get(
            cache_key,
            provider=provider.instance_id,
            category=CACHE_CATEGORY_ARTWORK_PROBE,
        )
    ) is not None:
        return bool(cached)

    try:
        async with (
            acquire_lms_request_slot(),
            provider.mass.http_session.get(
                url,
                timeout=ClientTimeout(total=ARTWORK_VALIDATION_TIMEOUT),
                headers=get_configured_basic_auth(provider),
            ) as response,
        ):
            if response.status != 200:
                result = False
            else:
                content_type = response.headers.get("Content-Type", "")
                result = "image/" in content_type.lower()
    except TimeoutError, ClientError:
        result = False

    await provider.mass.cache.set(
        cache_key,
        result,
        provider=provider.instance_id,
        category=CACHE_CATEGORY_ARTWORK_PROBE,
        expiration=ARTWORK_VALIDATION_CACHE_TTL,
    )
    return result


def get_thumb_path(item: Artist | Album) -> str | None:
    """Return first thumbnail image path for an item, if present."""
    if not item.metadata or not item.metadata.images:
        return None
    for image in item.metadata.images:
        if image.type == ImageType.THUMB:
            return image.path
    return None


def set_thumb_path(
    item: Artist | Album,
    path: str | None,
    provider_instance: str | None = None,
) -> None:
    """Set or clear thumbnail image path on an artist or album."""
    owner_provider = provider_instance or item.provider
    images = list(item.metadata.images or [])
    thumb_image = next(
        (img for img in images if img.type == ImageType.THUMB),
        None,
    )
    if path is None:
        if thumb_image is None:
            return
        item.metadata.images = UniqueList(img for img in images if img is not thumb_image)
        return
    if thumb_image is not None:
        new_image = dataclasses.replace(
            thumb_image,
            path=path,
            provider=owner_provider,
            remotely_accessible=False,
        )
        item.metadata.images = UniqueList(
            new_image if img is thumb_image else img for img in images
        )
        return
    item.metadata.images = UniqueList(
        [
            *images,
            MediaItemImage(
                type=ImageType.THUMB,
                path=path,
                provider=owner_provider,
                remotely_accessible=False,
            ),
        ]
    )


def append_artwork_cache_buster(
    provider: LyrionMusicProvider,
    url: str,
) -> str:
    """Append cache-buster token to artwork URLs when configured."""
    token = provider.get_setup_value(CONF_ARTWORK_CACHE_BUSTER)
    return pylyrion_artwork.append_artwork_cache_buster(url, token)
