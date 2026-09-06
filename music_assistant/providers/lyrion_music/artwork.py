"""Lyrion artwork and image resolution helpers."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import MediaItemImage, UniqueList

from music_assistant.providers.lyrion.client import get_configured_host, get_configured_port

from . import parsers
from .constants import ARTWORK_VALIDATION_TIMEOUT, CONF_ARTWORK_CACHE_BUSTER, RPC_TIMEOUT

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist

    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


async def resolve_image(provider: LyrionMusicProvider, path: str) -> str | bytes:
    """Resolve artist artwork URLs with LMS size fallback when needed."""
    if "image_600x600_f" not in path:
        return path
    if image_bytes := await fetch_remote_image_if_ok(provider, path):
        return image_bytes

    fallback_path = path.replace("image_600x600_f", "image_300x300_f")
    if fallback_path != path and (
        fallback_bytes := await fetch_remote_image_if_ok(provider, fallback_path)
    ):
        return fallback_bytes
    return path


async def build_artist(provider: LyrionMusicProvider, raw_artist: dict[str, Any]) -> Artist:
    """Build artist model from LMS payload and validate artwork URL."""
    return parsers.parse_artist(provider, raw_artist)


async def build_album(provider: LyrionMusicProvider, raw_album: dict[str, Any]) -> Album:
    """Build album model from LMS payload and validate artwork URL."""
    return parsers.parse_album(provider, raw_album)


async def ensure_preferred_artwork_size(
    provider: LyrionMusicProvider, item: Artist | Album
) -> tuple[str, ...]:
    """Resolve album or artist artwork by trying 600 first, then 300."""
    attempted_urls: list[str] = []
    if not item.metadata.images:
        return ()
    thumb_image = next(
        (img for img in item.metadata.images if img.type == ImageType.THUMB),
        None,
    )
    if thumb_image is None:
        return ()
    attempted_urls.append(thumb_image.path)
    if await probe_remote_image(provider, thumb_image.path):
        return tuple(attempted_urls)
    fallback_url = thumb_image.path.replace("600x600", "300x300")
    if fallback_url != thumb_image.path:
        attempted_urls.append(fallback_url)
    if fallback_url != thumb_image.path and await probe_remote_image(provider, fallback_url):
        new_image = dataclasses.replace(thumb_image, path=fallback_url)
        item.metadata.images = UniqueList(
            new_image if img is thumb_image else img for img in (item.metadata.images or [])
        )
        return tuple(attempted_urls)
    # Both URLs unreachable — drop the broken entry rather than storing it
    item.metadata.images = UniqueList(
        img for img in (item.metadata.images or []) if img is not thumb_image
    )
    return tuple(attempted_urls)


def extract_artwork_url(
    provider: LyrionMusicProvider,
    raw: dict[str, Any],
    fallback_id: str | None = None,
) -> str | None:
    """Extract an artwork URL from known LMS payload fields."""
    for key in ("artwork_url", "artwork", "icon"):
        if (value := raw.get(key)) is None:
            continue
        path = str(value).strip()
        if not path:
            continue
        if path.startswith(("http://", "https://")):
            return append_artwork_cache_buster(provider, path)
        if path.startswith("/"):
            return append_artwork_cache_buster(
                provider,
                to_lms_absolute_url(provider, path),
            )

    cover_id = raw.get("coverid")
    if cover_id is None:
        cover_id = raw.get("artwork_track_id")
    if cover_id is None:
        cover_id = fallback_id
    if cover_id is None:
        return None
    encoded_cover_id = quote(str(cover_id), safe="")
    return append_artwork_cache_buster(
        provider,
        to_lms_absolute_url(provider, f"/music/{encoded_cover_id}/cover_600x600_f"),
    )


def extract_artist_artwork_url(provider: LyrionMusicProvider, raw: dict[str, Any]) -> str | None:
    """Extract artist artwork URL from known LMS artist fields."""
    portrait_id = raw.get("portraitid")
    if portrait_id is not None:
        portrait_id_str = str(portrait_id).strip()
        if portrait_id_str:
            encoded_id = quote(portrait_id_str, safe="")
            return append_artwork_cache_buster(
                provider,
                to_lms_absolute_url(
                    provider,
                    f"/contributor/{encoded_id}/image_600x600_f",
                ),
            )

    for key in ("artwork_url", "artwork", "icon"):
        value = raw.get(key)
        if value is None:
            continue
        path = str(value).strip()
        if not path:
            continue
        path = normalize_artist_artwork_path(path)
        if path.startswith(("http://", "https://")):
            return append_artwork_cache_buster(provider, path)
        if path.startswith("/"):
            return append_artwork_cache_buster(
                provider,
                to_lms_absolute_url(provider, path),
            )

    artist_id = parsers.extract_item_id(
        raw,
        id_keys=("id", "artist_id", "contributor_id"),
    )
    if artist_id:
        encoded_artist_id = quote(artist_id, safe="")
        return append_artwork_cache_buster(
            provider,
            to_lms_absolute_url(
                provider,
                f"/imageproxy/mai/_artist/{encoded_artist_id}/image_600x600_f",
            ),
        )
    return None


def normalize_artist_artwork_path(path: str) -> str:
    """Normalize LMS artist artwork path variants to a sized endpoint."""
    base, separator, query = path.partition("?")
    if base.endswith("/image") and "/contributor/" in base:
        base = f"{base}_600x600_f"
    if separator:
        return f"{base}?{query}"
    return base


def to_lms_absolute_url(provider: LyrionMusicProvider, path: str) -> str:
    """Build an absolute LMS URL from a relative path."""
    host = get_configured_host(provider)
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider, default=None)
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    return f"http://{host}:{port}{path}"


async def fetch_remote_image_if_ok(provider: LyrionMusicProvider, url: str) -> bytes | None:
    """Fetch image bytes and return None for invalid LMS image responses."""
    try:
        async with provider.mass.http_session.get(
            url,
            timeout=ClientTimeout(total=RPC_TIMEOUT),
        ) as response:
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
    try:
        async with provider.mass.http_session.get(
            url,
            timeout=ClientTimeout(total=ARTWORK_VALIDATION_TIMEOUT),
        ) as response:
            if response.status != 200:
                return False
            content_type = response.headers.get("Content-Type", "")
            return "image/" in content_type.lower()
    except TimeoutError, ClientError:
        return False


def get_thumb_path(item: Artist | Album) -> str | None:
    """Return first thumbnail image path for an item, if present."""
    if not item.metadata or not item.metadata.images:
        return None
    for image in item.metadata.images:
        if image.type == ImageType.THUMB:
            return image.path
    return None


def set_thumb_path(item: Artist | Album, path: str | None) -> None:
    """Set or clear thumbnail image path on an artist or album."""
    images = list(item.metadata.images or [])
    thumb_image = next((img for img in images if img.type == ImageType.THUMB), None)
    if path is None:
        if thumb_image is None:
            return
        item.metadata.images = UniqueList(img for img in images if img is not thumb_image)
        return
    if thumb_image is not None:
        new_image = dataclasses.replace(thumb_image, path=path)
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
                provider=item.provider,
                remotely_accessible=True,
            ),
        ]
    )


def append_artwork_cache_buster(provider: LyrionMusicProvider, url: str) -> str:
    """Append cache-buster token to artwork URLs when configured."""
    token = provider.get_setup_value(CONF_ARTWORK_CACHE_BUSTER)
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}ma_lms_art={token}"
