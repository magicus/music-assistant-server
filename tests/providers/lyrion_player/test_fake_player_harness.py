"""
Lyrion player fake-harness tests.

This file re-exports the shared Lyrion slimproto tests so the standard test entrypoint
is the player directory itself: ``pytest tests/providers/lyrion_player``.
"""

from __future__ import annotations

from tests.providers.lyrion.test_fake_player_harness import *  # noqa: F403
