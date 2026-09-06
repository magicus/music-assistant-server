"""Setup flow for the Lyrion music provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.providers.lyrion.setup_flow import run_lms_setup_flow

from .constants import CONF_LMS_HOST, CONF_LMS_PORT, DEFAULT_LMS_PORT

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)


async def run_setup(session: SetupSession) -> None:
    """Run setup flow: collect connection details and create the provider."""
    await run_lms_setup_flow(
        session,
        current_domain="lyrion_music",
        host_key=CONF_LMS_HOST,
        port_key=CONF_LMS_PORT,
        default_port=DEFAULT_LMS_PORT,
        logger=LOGGER,
        log_prefix="Lyrion music",
    )
