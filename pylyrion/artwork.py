"""Shared Lyrion artwork URL and format helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

from pylyrion.cometd.constants import RPC_TIMEOUT
from pylyrion.lyrion_constants import ARTWORK_VALIDATION_CACHE_TTL, ARTWORK_VALIDATION_TIMEOUT
from pylyrion.session import build_lms_url

ArtworkItem = TypeVar("ArtworkItem")


async def ensure_preferred_artwork_size(
    item: ArtworkItem,
    probe_image: Callable[[str], Awaitable[bool]],
    get_thumb_path: Callable[[ArtworkItem], str | None],
    set_thumb_path: Callable[[ArtworkItem, str | None], None],
) -> tuple[str, ...]:
    """Keep 600px artwork when available, otherwise fall back to 300px."""
    thumb_path = get_thumb_path(item)
    if thumb_path is None:
        return ()

    attempted_urls = [thumb_path]
    if await probe_image(thumb_path):
        return tuple(attempted_urls)

    fallback_url = thumb_path.replace("600x600", "300x300")
    if fallback_url != thumb_path:
        attempted_urls.append(fallback_url)
    if fallback_url != thumb_path and await probe_image(fallback_url):
        set_thumb_path(item, fallback_url)
        return tuple(attempted_urls)

    set_thumb_path(item, None)
    return tuple(attempted_urls)


async def fetch_remote_image_if_ok(session: Any, url: str) -> bytes | None:
    """Fetch image bytes and return None for invalid LMS image responses."""
    try:
        async with session.get(
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


async def probe_remote_image(
    session: Any,
    cache: Any,
    provider_id: str,
    url: str,
    cache_category: str,
) -> bool:
    """Check a remote image endpoint without downloading payload bytes."""
    cache_key = f"probe::{url}"
    if (
        cached := await cache.get(
            cache_key,
            provider=provider_id,
            category=cache_category,
        )
    ) is not None:
        return bool(cached)

    try:
        async with session.get(
            url,
            timeout=ClientTimeout(total=ARTWORK_VALIDATION_TIMEOUT),
        ) as response:
            if response.status != 200:
                result = False
            else:
                content_type = response.headers.get("Content-Type", "")
                result = "image/" in content_type.lower()
    except TimeoutError, ClientError:
        result = False

    await cache.set(
        cache_key,
        result,
        provider=provider_id,
        category=cache_category,
        expiration=ARTWORK_VALIDATION_CACHE_TTL,
    )
    return result


def append_artwork_cache_buster(url: str, token: str | None) -> str:
    """Append cache-buster token to artwork URLs when configured."""
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}ma_lms_art={token}"


def normalize_artist_artwork_path(path: str) -> str:
    """Normalize LMS artist artwork path variants to a sized endpoint."""
    base, separator, query = path.partition("?")
    if base.endswith("/image") and "/contributor/" in base:
        base = f"{base}_600x600_f"
    if separator:
        return f"{base}?{query}"
    return base


def artwork_fallback_path(path: str) -> str | None:
    """Return preferred fallback path when 600px artwork is unavailable."""
    if "image_600x600_f" in path:
        return path.replace("image_600x600_f", "image_300x300_f")
    if "cover_600x600_f" in path:
        return path.replace("cover_600x600_f", "cover_300x300_f")
    return None


def extract_artwork_url(
    row: Mapping[str, str],
    *,
    host: str,
    port: int,
    fallback_id: str | None = None,
    cache_buster_token: str | None = None,
) -> str | None:
    """Extract album/track artwork URL from known LMS payload fields."""
    for key in ("artwork_url", "artwork", "icon"):
        if (value := row.get(key)) is None:
            continue
        if value.startswith(("http://", "https://")):
            return append_artwork_cache_buster(value, cache_buster_token)
        if value.startswith("/"):
            return append_artwork_cache_buster(
                to_lms_absolute_url(host, port, value),
                cache_buster_token,
            )

    cover_id = row.get("coverid") or row.get("artwork_track_id") or fallback_id
    if cover_id is None:
        return None
    encoded_cover_id = quote(cover_id, safe="")
    return append_artwork_cache_buster(
        to_lms_absolute_url(
            host,
            port,
            f"/music/{encoded_cover_id}/cover_600x600_f",
        ),
        cache_buster_token,
    )


def extract_artist_artwork_url(
    row: Mapping[str, str],
    *,
    host: str,
    port: int,
    fallback_artist_id: str | None = None,
    cache_buster_token: str | None = None,
) -> str | None:
    """Extract artist artwork URL from known LMS artist fields."""
    portrait_id = row.get("portraitid")
    if portrait_id is not None:
        encoded_id = quote(portrait_id, safe="")
        return append_artwork_cache_buster(
            to_lms_absolute_url(
                host,
                port,
                f"/contributor/{encoded_id}/image_600x600_f",
            ),
            cache_buster_token,
        )

    for key in ("artwork_url", "artwork", "icon"):
        value = row.get(key)
        if value is None:
            continue
        value = normalize_artist_artwork_path(value)
        if value.startswith(("http://", "https://")):
            return append_artwork_cache_buster(value, cache_buster_token)
        if value.startswith("/"):
            return append_artwork_cache_buster(
                to_lms_absolute_url(host, port, value),
                cache_buster_token,
            )

    if fallback_artist_id:
        encoded_artist_id = quote(fallback_artist_id, safe="")
        return append_artwork_cache_buster(
            to_lms_absolute_url(
                host,
                port,
                f"/imageproxy/mai/_artist/{encoded_artist_id}/image_600x600_f",
            ),
            cache_buster_token,
        )
    return None


def to_lms_absolute_url(host: str, port: int, path: str) -> str:
    """Build an absolute LMS URL from a relative path."""
    return build_lms_url(host, port, path)
