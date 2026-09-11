"""Shared Lyrion artwork URL and format helpers."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from pylyrion.session import build_lms_url


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
        to_lms_absolute_url(host, port, f"/music/{encoded_cover_id}/cover_600x600_f"),
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
            to_lms_absolute_url(host, port, f"/contributor/{encoded_id}/image_600x600_f"),
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
