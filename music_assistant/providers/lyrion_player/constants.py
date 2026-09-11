"""Constants for the Lyrion player provider."""

from music_assistant_models.enums import PlayerFeature, ProviderFeature

from music_assistant.constants import CONF_PORT

CONF_LMS_HOST = "lms_host"
CONF_LMS_PORT = CONF_PORT
CONF_FALLBACK_POLLING = "fallback_polling"
CONF_FALLBACK_POLLING_INTERVAL = "fallback_polling_interval"
DEFAULT_LMS_HOST = "127.0.0.1"
DEFAULT_LMS_PORT = 9000
DEFAULT_FALLBACK_POLLING_INTERVAL = 15
RPC_TIMEOUT = 10
PLAYERS_BATCH_SIZE = 200
COMETD_RETRY_DELAY = 5
COMETD_CONNECT_TIMEOUT = 35
COMETD_PLAYERSTATUS_TAGS = "tags:cdegilopqrstuyAABEGIKNPSTV"
COMETD_SERVERSTATUS_SUBSCRIBE_INTERVAL = 30
COMETD_SERVERSTATUS_BATCH_SIZE = 500

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.REMOVE_PLAYER,
}

PLAYER_SUPPORTED_FEATURES: set[PlayerFeature] = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.ENQUEUE,
    PlayerFeature.PAUSE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.POWER,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.SEEK,
}

# Queue sync tuning defaults.
MAX_SYNC_QUEUE_ITEMS = 500
SYNC_REBUILD_COST_THRESHOLD = 40
SYNC_REBUILD_RATIO_THRESHOLD = 0.3
