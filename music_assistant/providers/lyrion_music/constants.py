"""Constants for the Lyrion music provider."""

from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import CONF_PORT

CONF_LMS_HOST = "lms_host"
CONF_LMS_PORT = CONF_PORT
DEFAULT_LMS_HOST = "127.0.0.1"
DEFAULT_LMS_PORT = 9000
RPC_TIMEOUT = 10
ARTWORK_VALIDATION_TIMEOUT = 2
ARTWORK_WORKER_COUNT = 12
SEARCH_CACHE_TTL = 60
ITEM_CACHE_TTL = 3600
BATCH_LOOKUP_SIZE = 25
BROWSE_PAGE_SIZE = 250
ARTIST_TAGS = "tags:4abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALBUM_TAGS = "tags:abcdefghijklmnopqrstuvwxyz"
TRACK_TAGS = "tags:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
STREAM_PATH_TEMPLATE = "/music/{track_id}/download"
CONF_ARTWORK_CACHE_BUSTER = "artwork_cache_buster"
ACTION_ROTATE_ARTWORK_CACHE_TOKEN = "rotate_artwork_cache_token"

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
}
