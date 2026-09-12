"""Unit tests for the generic queue sync engine."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.lyrion_player.queue.queue_planner import (
    QueueMutation,
    QueueMutationType,
    QueuePlan,
)
from music_assistant.providers.lyrion_player.queue.queue_sync_engine import (
    QueueSyncEngine,
    QueueSyncInput,
)


@pytest.mark.asyncio
async def test_apply_returns_early_when_source_and_target_identities_match() -> None:
    """No planner/executor work is needed when identities are already in sync."""
    planner = MagicMock()
    executor = SimpleNamespace(
        rebuild_from=AsyncMock(),
        delete_index=AsyncMock(),
        move_index=AsyncMock(),
        insert_entry_at=AsyncMock(),
    )
    engine = QueueSyncEngine(planner)

    sync_input = QueueSyncInput(
        source_entries=("a",),
        source_identities=(("url", "a"),),
        target_identities=(("url", "a"),),
        protected_prefix_len=0,
    )

    await engine.apply(sync_input, executor)

    planner.plan.assert_not_called()
    executor.rebuild_from.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_uses_rebuild_plan_when_planner_requests_it() -> None:
    """A rebuild plan should call only executor.rebuild_from."""
    planner = MagicMock(return_value=None)
    planner.plan = MagicMock(return_value=QueuePlan(mutations=(), rebuild_from_index=2))
    executor = SimpleNamespace(
        rebuild_from=AsyncMock(),
        delete_index=AsyncMock(),
        move_index=AsyncMock(),
        insert_entry_at=AsyncMock(),
    )
    engine = QueueSyncEngine(planner)

    source_entries = ("a", "b", "c")
    sync_input = QueueSyncInput(
        source_entries=source_entries,
        source_identities=(("url", "a"), ("url", "b"), ("url", "c")),
        target_identities=(("url", "a"),),
        protected_prefix_len=0,
    )

    await engine.apply(sync_input, executor)

    executor.rebuild_from.assert_awaited_once_with(source_entries, 2)
    executor.delete_index.assert_not_awaited()
    executor.move_index.assert_not_awaited()
    executor.insert_entry_at.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_executes_mutations_and_skips_invalid_indexes() -> None:
    """Mutation execution should handle delete/move/insert and skip incomplete ones."""
    planner = MagicMock()
    planner.plan = MagicMock(
        return_value=QueuePlan(
            mutations=(
                QueueMutation(QueueMutationType.DELETE, index=5),
                QueueMutation(QueueMutationType.MOVE, index=4, to_index=1),
                QueueMutation(QueueMutationType.MOVE, index=4, to_index=None),
                QueueMutation(QueueMutationType.INSERT_FROM_SOURCE, index=2, source_index=0),
                QueueMutation(QueueMutationType.INSERT_FROM_SOURCE, index=3, source_index=None),
            ),
            rebuild_from_index=None,
        )
    )
    executor = SimpleNamespace(
        rebuild_from=AsyncMock(),
        delete_index=AsyncMock(),
        move_index=AsyncMock(),
        insert_entry_at=AsyncMock(),
    )
    engine = QueueSyncEngine(planner)

    sync_input = QueueSyncInput(
        source_entries=("a", "b"),
        source_identities=(("url", "a"), ("url", "b")),
        target_identities=(("url", "x"),),
        protected_prefix_len=0,
    )

    await engine.apply(sync_input, executor)

    executor.delete_index.assert_awaited_once_with(5)
    executor.move_index.assert_awaited_once_with(4, 1)
    executor.insert_entry_at.assert_awaited_once_with("a", 2)
