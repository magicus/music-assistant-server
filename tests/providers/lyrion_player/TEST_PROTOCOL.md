# Lyrion Robustness Test Protocol

This protocol checks how a `lyrion_player` instance behaves when CometD updates are delayed or missing, while LMS state changes elsewhere.

It works against either:

- the in-memory fake LMS backend used by the test harness, or
- the on-demand Docker LMS backend started by the shared Lyrion fixtures.

## Prerequisites

- Music Assistant server running with the `lyrion_player` provider configured and enabled.
- One LMS player connected to that provider.
- Python available in the repo virtualenv, for example `.venv/bin/python`.

## Helper Commands

The probe script prints a stable JSON snapshot or sends one control command through LMS JSON-RPC:

```bash
.venv/bin/python tests/providers/lyrion_player/lyrion_robustness_probe.py \
  --base-url http://127.0.0.1:9000 \
  --player-id <player-id> snapshot
```

To make a controlled external change from a second terminal:

```bash
.venv/bin/python tests/providers/lyrion_player/lyrion_robustness_probe.py \
  --base-url http://127.0.0.1:9000 \
  --player-id <player-id> command \
  --action load-play \
  --track-id <track-id>
```

You can use the `track_id` from the `titles` result in the snapshot output, or any other track that exists in LMS.

## Manual Test 1: Network Drop While Another Controller Changes State

1. Run the snapshot command above and note the current `playerstatus.mode`, `playlist_cur_index`, `playlist_tracks`, and `serverstatus.players_loop` values.
2. Start playback with the command helper or with a second controller.
3. Break connectivity between Music Assistant and LMS.
4. While the connection is broken, change the queue or playback state from the second controller.
5. Restore connectivity.
6. Run the snapshot command again.

Expected result:

- The snapshot should eventually reflect the state that LMS currently reports.
- If fallback polling is enabled on the provider, the state should converge even if CometD did not deliver every update.
- The reported player should remain consistent with the serverstatus roster and should not stay permanently stuck in the old state.

## Manual Test 2: CometD Goes Quiet But JSON-RPC Still Answers

1. Run the snapshot command once and confirm the player is connected.
2. Leave playback running or start it with the command helper.
3. Interrupt only the event stream path if you can do so in your environment, while keeping JSON-RPC reachable.
4. Make an external playback change from the second controller.
5. Run the snapshot command again.

Expected result:

- The JSON-RPC snapshot should show the new playback state.
- The provider should not require a full manual reload to catch up.
- If fallback polling is enabled, the next poll cycle should converge the MA player to the same state.

## Automation Reference

The following tests cover the same behavior in code:

- `tests/providers/lyrion_player/test_cometd_stream.py`
- `tests/providers/lyrion_player/test_cometd_recovery.py`

Run them with:

```bash
.venv/bin/python -m pytest tests/providers/lyrion_player/test_cometd_stream.py tests/providers/lyrion_player/test_cometd_recovery.py -q
```
