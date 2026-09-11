#!/usr/bin/env bash
set -euo pipefail

# Run Docker-managed live Lyrion integration tests.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$REPO_ROOT"
pytest tests/providers/lyrion_music/test_provider_live_smoke.py \
	--live-lyrion-docker -q "$@"
