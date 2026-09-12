"""Shared Lyrion constants used across pylyrion and MA provider layers."""

from __future__ import annotations

ARTWORK_VALIDATION_TIMEOUT = 2
ARTWORK_VALIDATION_CACHE_TTL = 3600 * 6
SEARCH_CACHE_TTL = 60
ITEM_CACHE_TTL = 3600
BATCH_LOOKUP_SIZE = 25
ARTIST_TAGS = "tags:4abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALBUM_TAGS = "tags:abcdefghijklmnopqrstuvwxyz"
TRACK_TAGS = "tags:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
STREAM_PATH_TEMPLATE = "/music/{track_id}/download"
CONF_ARTWORK_CACHE_BUSTER = "artwork_cache_buster"
