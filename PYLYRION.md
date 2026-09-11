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
