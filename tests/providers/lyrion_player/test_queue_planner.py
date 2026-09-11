"""Unit tests for Lyrion queue diff planning."""

from __future__ import annotations

from music_assistant.providers.lyrion_player.queue.queue_planner import (
    QueueDiffPlanner,
    QueueMutation,
    QueueMutationType,
)


def test_plan_returns_no_mutations_for_identical_sequences() -> None:
    """Equal sequences should produce an empty plan."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=10, rebuild_ratio_threshold=1.0)
    identities = (("track", "a"), ("track", "b"))

    plan = planner.plan(identities, identities, protected_prefix_len=0)

    assert plan.mutations == ()
    assert plan.rebuild_from_index is None


def test_plan_emits_move_insert_and_delete_mutations() -> None:
    """Planner should prefer delta mutations when they are cheaper than rebuild."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=10, rebuild_ratio_threshold=1.0)
    source = (("track", "a"), ("track", "b"), ("track", "c"), ("track", "d"))
    target = (("track", "a"), ("track", "c"), ("track", "e"), ("track", "b"))

    plan = planner.plan(source, target, protected_prefix_len=0)

    assert plan.rebuild_from_index is None
    assert plan.mutations == (
        QueueMutation(QueueMutationType.MOVE, 3, 1, None),
        QueueMutation(QueueMutationType.INSERT_FROM_SOURCE, 3, None, 3),
        QueueMutation(QueueMutationType.DELETE, 4, None, None),
    )


def test_plan_rebuilds_from_zero_when_protected_prefix_diverges() -> None:
    """A mismatch inside the protected prefix should force a full rebuild."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=10, rebuild_ratio_threshold=1.0)
    source = (("track", "a"), ("track", "b"))
    target = (("track", "x"), ("track", "b"))

    plan = planner.plan(source, target, protected_prefix_len=1)

    assert plan.mutations == ()
    assert plan.rebuild_from_index == 0


def test_plan_rebuilds_from_protected_prefix_when_cost_is_high() -> None:
    """Large diffs should switch to rebuilding from the protected prefix."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=1, rebuild_ratio_threshold=1.0)
    source = (("track", "a"), ("track", "b"), ("track", "c"))
    target = (("track", "a"), ("track", "x"), ("track", "y"))

    plan = planner.plan(source, target, protected_prefix_len=1)

    assert plan.mutations == ()
    assert plan.rebuild_from_index == 1


def test_plan_rebuilds_from_protected_prefix_when_cost_ratio_is_high() -> None:
    """Relative diff cost should also trigger a rebuild."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=10, rebuild_ratio_threshold=0.25)
    source = (("track", "a"), ("track", "b"), ("track", "c"), ("track", "d"))
    target = (("track", "a"), ("track", "x"), ("track", "y"), ("track", "z"))

    plan = planner.plan(source, target, protected_prefix_len=1)

    assert plan.mutations == ()
    assert plan.rebuild_from_index == 1


def test_plan_clamps_oversized_protected_prefix_before_rebuild() -> None:
    """Protected prefix should never exceed queue overlap when rebuilding."""
    planner = QueueDiffPlanner(rebuild_cost_threshold=0, rebuild_ratio_threshold=0.0)
    source = (("track", "a"), ("track", "b"))
    target = (("track", "a"), ("track", "x"), ("track", "y"))

    plan = planner.plan(source, target, protected_prefix_len=10)

    assert plan.mutations == ()
    assert plan.rebuild_from_index == 0
