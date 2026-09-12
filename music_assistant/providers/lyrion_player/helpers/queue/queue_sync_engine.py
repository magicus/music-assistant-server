"""Generic queue synchronization engine reusable by protocol adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .queue_planner import QueueDiffPlanner, QueueMutationType

EntryT = TypeVar("EntryT")
ExecutorEntryT_contra = TypeVar("ExecutorEntryT_contra", contravariant=True)


@dataclass(frozen=True)
class QueueSyncInput(Generic[EntryT]):  # noqa: UP046
    """Source/target queue data passed into the sync engine."""

    source_entries: tuple[EntryT, ...]
    source_identities: tuple[tuple[str, str], ...]
    target_identities: tuple[tuple[str, str], ...]
    protected_prefix_len: int


class QueueMutationExecutor(Protocol[ExecutorEntryT_contra]):
    """Adapter that executes queue mutation primitives on a target system."""

    async def rebuild_from(
        self,
        source_entries: tuple[ExecutorEntryT_contra, ...],
        index: int,
    ) -> None:
        """Rebuild target queue from index to end."""

    async def delete_index(self, index: int) -> None:
        """Delete one item from target queue by index."""

    async def move_index(self, from_index: int, to_index: int) -> None:
        """Move one target queue item from one index to another."""

    async def insert_entry_at(
        self,
        entry: ExecutorEntryT_contra,
        index: int,
    ) -> None:
        """Insert one source entry into target queue at index."""


class QueueSyncEngine(Generic[EntryT]):  # noqa: UP046
    """Apply planner output using a target-specific mutation executor."""

    def __init__(self, planner: QueueDiffPlanner) -> None:
        """
        Initialize sync engine.

        :param planner: Queue planner that decides delta vs rebuild.
        """
        self._planner = planner

    async def apply(
        self,
        sync_input: QueueSyncInput[EntryT],
        executor: QueueMutationExecutor[EntryT],
    ) -> None:
        """
        Apply source queue identities to target queue through executor.

        :param sync_input: Source/target identities and source entries.
        :param executor: Target adapter executing concrete mutations.
        """
        if sync_input.source_identities == sync_input.target_identities:
            return

        plan = self._planner.plan(
            source_identities=sync_input.source_identities,
            target_identities=sync_input.target_identities,
            protected_prefix_len=sync_input.protected_prefix_len,
        )

        if plan.rebuild_from_index is not None:
            await executor.rebuild_from(
                sync_input.source_entries,
                plan.rebuild_from_index,
            )
            return

        for mutation in plan.mutations:
            if mutation.mutation_type == QueueMutationType.DELETE:
                await executor.delete_index(mutation.index)
                continue
            if mutation.mutation_type == QueueMutationType.MOVE:
                if mutation.to_index is None:
                    continue
                await executor.move_index(mutation.index, mutation.to_index)
                continue
            if mutation.mutation_type == QueueMutationType.INSERT_FROM_SOURCE:
                if mutation.source_index is None:
                    continue
                await executor.insert_entry_at(
                    sync_input.source_entries[mutation.source_index],
                    mutation.index,
                )
