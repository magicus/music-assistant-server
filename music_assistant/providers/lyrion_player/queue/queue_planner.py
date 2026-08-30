"""Queue diff planning primitives used by the Lyrion queue synchronizer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class QueueMutationType(StrEnum):
    """Supported queue mutation operations for the planner output."""

    DELETE = "delete"
    MOVE = "move"
    INSERT_FROM_SOURCE = "insert_from_source"


@dataclass(frozen=True)
class QueueMutation:
    """One queued mutation produced by the planner."""

    mutation_type: QueueMutationType
    index: int
    to_index: int | None = None
    source_index: int | None = None


@dataclass(frozen=True)
class QueuePlan:
    """Plan that transforms one queue identity sequence into another."""

    mutations: tuple[QueueMutation, ...]
    rebuild_from_index: int | None = None


class QueueDiffPlanner:
    """Compute delta plans and decide when a rebuild is cheaper."""

    def __init__(
        self,
        rebuild_cost_threshold: int,
        rebuild_ratio_threshold: float,
    ) -> None:
        """
        Initialize planner thresholds.

        :param rebuild_cost_threshold: Absolute operation-cost threshold.
        :param rebuild_ratio_threshold: Relative operation-cost threshold.
        """
        self._rebuild_cost_threshold = rebuild_cost_threshold
        self._rebuild_ratio_threshold = rebuild_ratio_threshold

    def plan(
        self,
        source_identities: Sequence[tuple[str, str]],
        target_identities: Sequence[tuple[str, str]],
        protected_prefix_len: int,
    ) -> QueuePlan:
        """
        Return a delta plan for queue identity synchronization.

        The plan transforms target identities into source identities.

        :param source_identities: Desired queue identities.
        :param target_identities: Current queue identities.
        :param protected_prefix_len: Prefix that should be left untouched
            when possible.
        """
        protected_prefix_len = max(protected_prefix_len, 0)

        # If already-passed/buffered prefix diverged, do a full rebuild to
        # avoid fragile mutation logic around uncertain identity there.
        prefix_to_validate = min(
            protected_prefix_len,
            len(source_identities),
            len(target_identities),
        )
        for index in range(prefix_to_validate):
            if source_identities[index] != target_identities[index]:
                return QueuePlan(mutations=(), rebuild_from_index=0)

        operation_cost = self._estimate_operation_cost(
            source_identities,
            target_identities,
        )
        queue_length = max(1, len(source_identities))
        if (
            operation_cost > self._rebuild_cost_threshold
            or (operation_cost / queue_length) > self._rebuild_ratio_threshold
        ):
            return QueuePlan(
                mutations=(),
                rebuild_from_index=protected_prefix_len,
            )

        mutations = self._build_delta_mutations(
            source_identities,
            target_identities,
            protected_prefix_len,
        )
        return QueuePlan(mutations=tuple(mutations), rebuild_from_index=None)

    @staticmethod
    def _estimate_operation_cost(
        source_identities: Sequence[tuple[str, str]],
        target_identities: Sequence[tuple[str, str]],
    ) -> int:
        """Return a rough diff cost as mismatches + adds + deletes."""
        overlap = min(len(source_identities), len(target_identities))
        mismatches = sum(
            1 for index in range(overlap) if source_identities[index] != target_identities[index]
        )
        adds = max(0, len(source_identities) - len(target_identities))
        deletes = max(0, len(target_identities) - len(source_identities))
        return mismatches + adds + deletes

    def _build_delta_mutations(
        self,
        source_identities: Sequence[tuple[str, str]],
        target_identities: Sequence[tuple[str, str]],
        protected_prefix_len: int,
    ) -> list[QueueMutation]:
        """Build ordered mutations by simulating queue edits in-memory."""
        work = list(target_identities)
        mutations: list[QueueMutation] = []

        for target_index in range(
            protected_prefix_len,
            len(source_identities),
        ):
            desired_identity = source_identities[target_index]
            if target_index < len(work) and work[target_index] == desired_identity:
                continue

            found_index = None
            for candidate_index in range(target_index + 1, len(work)):
                if work[candidate_index] == desired_identity:
                    found_index = candidate_index
                    break

            if found_index is not None:
                work.insert(target_index, work.pop(found_index))
                mutations.append(
                    QueueMutation(
                        mutation_type=QueueMutationType.MOVE,
                        index=found_index,
                        to_index=target_index,
                    )
                )
                continue

            # Insert by appending and moving, which works even without explicit
            # insert-at-index support on the target protocol.
            work.append(desired_identity)
            inserted_from = len(work) - 1
            work.insert(target_index, work.pop(inserted_from))
            mutations.append(
                QueueMutation(
                    mutation_type=QueueMutationType.INSERT_FROM_SOURCE,
                    index=target_index,
                    source_index=target_index,
                )
            )

        for delete_index in range(
            len(work) - 1,
            len(source_identities) - 1,
            -1,
        ):
            mutations.append(
                QueueMutation(
                    mutation_type=QueueMutationType.DELETE,
                    index=delete_index,
                )
            )

        return mutations
