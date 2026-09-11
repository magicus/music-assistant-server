# Pylyrion Plan

## First slice

1. Extract the shared JSON-RPC transport into `pylyrion` with a neutral session object and error types.
2. Add a raw library browse API in `pylyrion` for paged entity reads.
3. Add a raw player control API in `pylyrion` for status reads.
4. Keep MA adapters thin by mapping MA config/models to and from `pylyrion`.
5. Duplicate the relevant tests: keep MA tests and add `pylyrion`-level tests for the same concepts.

## Second slice

1. Move raw id-discovery, lookup, and search helpers into `pylyrion` without changing their LMS semantics.
2. Point the MA music adapter at those raw `pylyrion` helpers while keeping MA parsing and model construction local.
3. Add duplicated pylyrion tests for raw lookup/search paths and keep the MA tests that cover the adapter layer.

## Open choices

- Whether to move lookup/search helpers into `pylyrion` in the next slice or keep them in MA until more of the model layer is ready.
- Whether to add a dedicated `pylyrion` packaging config beyond the root workspace setup once the split stabilizes.
- Whether the MA player adapter should keep creating a fresh `LyrionSession` per transport call or hold a cached pylyrion client/session per provider instance.
	- Chosen now: create a fresh pylyrion client per call to keep lifecycle coupling low and avoid hidden stale-session state while APIs are still moving.
	- Alternative A: cache one client on provider load for lower call overhead.
	- Alternative B: cache lazily with explicit invalidation on provider reload.

- Whether queue-status reads and sync-group commands should continue as raw command arrays in MA queue/player code or become named pylyrion operations.
	- Chosen now: expose them as named pylyrion player operations (`get_queue_status`, `sync_to`, `unsync`) and keep MA as a mapping adapter.
	- Alternative A: keep raw command arrays in MA for now and only move transport mechanics.
	- Alternative B: hide queue-status behind a future pylyrion queue domain object once more queue semantics move out of MA.

- Whether queue-mutation commands should stay as raw arrays in MA queue sync or become named pylyrion player operations.
	- Chosen now: move index/repeat/shuffle/clear/track-id-add/move/delete to named pylyrion operations and keep queue_diff logic in MA.
	- Alternative A: move only read operations and keep mutation commands raw until full queue domain extraction.
	- Alternative B: introduce a dedicated pylyrion QueueClient abstraction before exposing these methods on PlayerClient.

- Whether URL-entry metadata fallback (`playlistcontrol` -> `playlist add`) should move into pylyrion now.
	- Previous choice: keep fallback in MA queue adapter while queue mutation APIs were still being extracted.
	- Current choice: moved fallback into pylyrion via `add_url_to_queue`; MA now delegates.
	- Alternative A: later move URL operations into a dedicated pylyrion queue facade if that API shape becomes clearer.

## Subsequent slices

- URL queue add fallback moved from MA adapter into pylyrion as a named player operation (`add_url_to_queue`) to keep MA queue sync transport-thin.
	- Current split: pylyrion owns LMS metadata-command fallback behavior; MA adapter passes through queue metadata fields.
	- Future option: introduce a dedicated pylyrion queue facade once larger queue domain pieces leave MA.
