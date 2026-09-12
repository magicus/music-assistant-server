"""Generic library sync orchestration helpers for pylyrion clients."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class LibrarySyncHooks:
    """Describe callback hooks required to run one library sync pass."""

    iter_items: Callable[[], AsyncGenerator[Any]]
    get_sync_details: Callable[[Any], Awaitable[Any | None]]
    apply_item: Callable[[Any, Any | None, bool, set[int]], Awaitable[None]]
    on_item_status: Callable[[int, str], None]
    on_item_failure: Callable[[Any, Exception, int | None, set[int]], None]
    item_id_getter: Callable[[Any], str]
    item_name_getter: Callable[[Any], str]
    sync_details_item_id_getter: Callable[[Any], int]
    needs_update: Callable[[Any, Any], bool]
    on_post_item_failure: Callable[[Any, Exception], None] | None = None
    extra_needs_update: Callable[[Any, Any, Any], Awaitable[bool]] | None = None
    lookup_library_items: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None
    post_item_sync: Callable[[Any], Awaitable[None]] | None = None
    is_item_available: Callable[[Any], bool] | None = None
    skip_if_new_and_unavailable: bool = False
    pending_batch_size: int = 200
    handled_exceptions: tuple[type[Exception], ...] = ()


async def run_library_sync(hooks: LibrarySyncHooks) -> set[int]:
    """Run a generic library sync pass using callback hooks."""
    if not hooks.handled_exceptions:
        raise ValueError("LibrarySyncHooks.handled_exceptions may not be empty")

    cur_db_ids: set[int] = set()
    item_count = 0
    pending_extra_checks: list[tuple[Any, Any]] = []

    async for prov_item in hooks.iter_items():
        item_count += 1
        hooks.on_item_status(item_count, hooks.item_name_getter(prov_item))
        try:
            queued_for_extra = await _process_sync_item(
                hooks,
                prov_item,
                cur_db_ids,
                pending_extra_checks,
            )
        except hooks.handled_exceptions:
            continue
        if queued_for_extra:
            if len(pending_extra_checks) >= hooks.pending_batch_size:
                await _flush_pending_extra_checks(hooks, pending_extra_checks, cur_db_ids)
            continue
        await _run_post_item_sync(hooks, prov_item, None, cur_db_ids)

    await _flush_pending_extra_checks(hooks, pending_extra_checks, cur_db_ids)
    return cur_db_ids


async def _process_sync_item(
    hooks: LibrarySyncHooks,
    prov_item: Any,
    cur_db_ids: set[int],
    pending_extra_checks: list[tuple[Any, Any]],
) -> bool:
    """Process one item and return True when deferred for extra update checks."""
    db_id: int | None = None
    try:
        sync_details = await hooks.get_sync_details(prov_item)
        db_id = (
            hooks.sync_details_item_id_getter(sync_details) if sync_details is not None else None
        )
        if _should_skip_unavailable_item(hooks, prov_item, sync_details):
            return False

        needs_update = bool(sync_details and hooks.needs_update(sync_details, prov_item))
        if not needs_update and hooks.extra_needs_update is not None and sync_details is not None:
            pending_extra_checks.append((sync_details, prov_item))
            return True

        await hooks.apply_item(prov_item, sync_details, needs_update, cur_db_ids)
        return False
    except hooks.handled_exceptions as err:
        hooks.on_item_failure(prov_item, err, db_id, cur_db_ids)
        raise


def _should_skip_unavailable_item(
    hooks: LibrarySyncHooks,
    prov_item: Any,
    sync_details: Any | None,
) -> bool:
    """Return True when a new unavailable item should be skipped."""
    return bool(
        hooks.skip_if_new_and_unavailable
        and sync_details is None
        and hooks.is_item_available is not None
        and not hooks.is_item_available(prov_item)
    )


async def _run_post_item_sync(
    hooks: LibrarySyncHooks,
    prov_item: Any,
    db_id: int | None,
    cur_db_ids: set[int],
) -> None:
    """Run optional post-sync callback with dedicated error routing."""
    if hooks.post_item_sync is None:
        return
    try:
        await hooks.post_item_sync(prov_item)
    except hooks.handled_exceptions as err:
        if hooks.on_post_item_failure is not None:
            hooks.on_post_item_failure(prov_item, err)
        else:
            hooks.on_item_failure(prov_item, err, db_id, cur_db_ids)


async def _flush_pending_extra_checks(
    hooks: LibrarySyncHooks,
    pending_extra_checks: list[tuple[Any, Any]],
    cur_db_ids: set[int],
) -> None:
    """Apply items queued for extra update checks in one batch."""
    if not pending_extra_checks:
        return
    if hooks.lookup_library_items is None or hooks.extra_needs_update is None:
        pending_extra_checks.clear()
        return

    lookup_ids = [hooks.item_id_getter(prov_item) for _, prov_item in pending_extra_checks]
    library_items_by_provider_id = await hooks.lookup_library_items(lookup_ids)

    for sync_details, prov_item in pending_extra_checks:
        db_id = hooks.sync_details_item_id_getter(sync_details)
        try:
            item_id = hooks.item_id_getter(prov_item)
            if not (library_item := library_items_by_provider_id.get(item_id)):
                needs_update = True
            else:
                needs_update = await hooks.extra_needs_update(sync_details, prov_item, library_item)

            await hooks.apply_item(prov_item, sync_details, needs_update, cur_db_ids)
            await _run_post_item_sync(hooks, prov_item, db_id, cur_db_ids)
        except hooks.handled_exceptions as err:
            hooks.on_item_failure(prov_item, err, db_id, cur_db_ids)

    pending_extra_checks.clear()
