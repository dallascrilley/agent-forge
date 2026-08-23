"""Restart and unknown-effect matrix for the scripted backend contract."""

from __future__ import annotations

import pytest

from forge.orchestrator.fake_backend import (
    FakeBackend,
    LaunchCoordinator,
    TransientLaunchError,
    UnknownEffectError,
)


def test_never_started_and_active_effects_reconcile_across_restart():
    backend = FakeBackend()
    assert backend.reconcile("run-1/worker/launch/1").state == "never-started"
    first = LaunchCoordinator(backend).launch("run-1/worker/launch/1")
    second = LaunchCoordinator(backend).launch("run-1/worker/launch/1")

    assert first.status == second.status == "started"
    assert first.handle == second.handle
    assert backend.reconcile("run-1/worker/launch/1").state == "active"
    assert backend.launch_calls == ["run-1/worker/launch/1"] * 2


def test_settled_effect_is_not_launched_again_after_restart():
    backend = FakeBackend()
    key = "run-1/worker/launch/1"
    handle = LaunchCoordinator(backend).launch(key).handle
    assert handle is not None
    backend.settle(key)

    restarted = LaunchCoordinator(backend)
    decision = restarted.launch(key)
    observation = restarted.recover(key)
    assert decision.handle == handle
    assert observation.state == "settled"
    assert observation.result == "completed"


def test_only_one_pre_turn_transient_failure_retries():
    key = "run-1/worker/launch/1"
    backend = FakeBackend({key: ["pre-turn-transient", "started"]})
    decision = LaunchCoordinator(backend).launch(key)
    assert decision.status == "started"
    assert decision.retries == 1
    assert len(backend.launch_calls) == 2

    always_fails = FakeBackend({key: ["pre-turn-transient", "pre-turn-transient"]})
    with pytest.raises(TransientLaunchError):
        LaunchCoordinator(always_fails).launch(key)
    assert len(always_fails.launch_calls) == 2


def test_post_turn_failure_is_ambiguous_and_never_retried():
    key = "run-1/worker/launch/1"
    backend = FakeBackend({key: ["post-turn-transient"]})
    coordinator = LaunchCoordinator(backend)
    with pytest.raises(TransientLaunchError, match="post-turn"):
        coordinator.launch(key)
    assert len(backend.launch_calls) == 1

    restarted = LaunchCoordinator(backend)
    observation = restarted.recover(key)
    assert observation.state == "ambiguous"
    with pytest.raises(UnknownEffectError):
        backend.launch(key)
    assert len(backend.launch_calls) == 2


def test_explicit_ambiguous_effect_remains_blocked_pending_reconciliation():
    key = "run-1/worker/launch/1"
    backend = FakeBackend({key: ["ambiguous"]})
    decision = LaunchCoordinator(backend).launch(key)
    assert decision.status == "ambiguous"
    assert decision.handle is None
    assert decision.observation is not None
    assert decision.observation.state == "ambiguous"
    assert LaunchCoordinator(backend).recover(key).state == "ambiguous"


def test_backend_errors_other_than_pre_turn_transient_never_retry():
    class BrokenBackend(FakeBackend):
        def launch(self, idempotency_key):
            self.launch_calls.append(idempotency_key)
            raise RuntimeError("tool failure")

    backend = BrokenBackend()
    with pytest.raises(RuntimeError, match="tool failure"):
        LaunchCoordinator(backend).launch("run-1/worker/launch/1")
    assert backend.launch_calls == ["run-1/worker/launch/1"]
