"""Constants for the Lyrion music provider."""

from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import CONF_PORT

CONF_LMS_HOST = "lms_host"
CONF_LMS_PORT = CONF_PORT
DEFAULT_LMS_HOST = "127.0.0.1"
DEFAULT_LMS_PORT = 9000
ARTWORK_WORKER_COUNT = 6
BROWSE_PAGE_SIZE = 250
ACTION_RESCAN_ARTWORK = "rescan_artwork"

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
}
