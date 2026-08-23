"""Deterministic DAG readiness, limits, timeout, and review insertion tests."""

from __future__ import annotations

import pytest

from forge.orchestrator.scheduler import Scheduler, SchedulerError, validate_plan


def node(node_id, *, deps=(), profile="observe", repo="repo", timeout=10):
    return {
        "nodeId": node_id,
        "workerId": node_id,
        "dependencies": list(deps),
        "permissionProfile": profile,
        "repositoryId": repo,
        "timeoutSeconds": timeout,
    }


def test_ready_frontier_parallelism_and_dependency_blocking():
    # Unknown references are rejected before any worker can launch.
    with pytest.raises(SchedulerError, match="unknown dependency"):
        Scheduler(
            [
                node("a"),
                node("b"),
                node("c", deps=("a",)),
                node("d", deps=("missing",)),
            ],
            max_concurrency=3,
        )

    scheduler = Scheduler([node("a"), node("b"), node("c", deps=("a",))])
    assert [item.spec.node_id for item in scheduler.ready()] == ["a", "b"]
    assert [item.spec.node_id for item in scheduler.start_ready(now=10)] == ["a", "b"]
    scheduler.complete("a", "completed")
    scheduler.complete("b", "completed")
    assert [item.spec.node_id for item in scheduler.ready()] == ["c"]


def test_global_concurrency_and_one_mutation_per_repository():
    scheduler = Scheduler(
        [
            node("m1", profile="modify-isolated", repo="repo-a"),
            node("m2", profile="modify-isolated", repo="repo-a"),
            node("m3", profile="modify-isolated", repo="repo-b"),
            node("read", repo="repo-a"),
        ]
    )
    started = scheduler.start_ready(now=5)
    assert [item.spec.node_id for item in started] == ["m1", "m3", "read"]
    assert scheduler.ready()[0].spec.node_id == "m2"


def test_mutation_inserts_deterministic_review_and_partial_review_can_run():
    scheduler = Scheduler([node("implementation", profile="modify-isolated", timeout=4)])
    scheduler.start_ready(now=10)
    scheduler.complete("implementation", "partial", evidence=("partial diff",))

    assert [item.spec.node_id for item in scheduler.ready()] == ["implementation.review"]
    review = scheduler.start_ready(now=11)[0]
    assert review.spec.permission_profile == "review"
    assert review.spec.dependencies == ("implementation",)
    scheduler.complete("implementation.review", "verified-with-caveats", evidence=("review",))
    assert scheduler.snapshot()[-1]["verdict"] == "verified-with-caveats"


def test_timeout_returns_partial_evidence_and_blocks_non_review_dependents():
    scheduler = Scheduler(
        [
            node("work", timeout=3),
            node("downstream", deps=("work",)),
        ]
    )
    scheduler.start_ready(now=10)
    expired = scheduler.tick(now=13)
    assert [item.spec.node_id for item in expired] == ["work"]
    assert expired[0].status == "partial"
    assert "timed out" in expired[0].evidence[0]
    assert scheduler.ready() == ()
    downstream = next(item for item in scheduler.snapshot() if item["nodeId"] == "downstream")
    assert downstream["status"] == "blocked"


def test_cancellation_and_child_creation_fail_closed():
    scheduler = Scheduler([node("work")])
    scheduler.cancel("work", "operator cancelled")
    assert scheduler.snapshot()[0]["status"] == "cancelled"
    with pytest.raises(SchedulerError, match="cannot be cancelled"):
        scheduler.cancel("work")
    with pytest.raises(SchedulerError, match="cannot create child"):
        scheduler.add_node(node("child", deps=("work",)))


def test_cycles_duplicate_nodes_and_invalid_limits_are_rejected():
    with pytest.raises(SchedulerError, match="cycle"):
        validate_plan([node("a", deps=("b",)), node("b", deps=("a",))])
    with pytest.raises(SchedulerError, match="duplicate"):
        validate_plan([node("a"), node("a")])
    with pytest.raises(SchedulerError, match="reserved review"):
        validate_plan([node("a", profile="modify-isolated"), node("a.review")])
    with pytest.raises(SchedulerError, match="between 1 and 3"):
        Scheduler([node("a")], max_concurrency=4)
