"""Scripted backend contract for restart and unknown-effect tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol


class BackendError(RuntimeError):
    """Base class for scripted backend failures."""


class TransientLaunchError(BackendError):
    """A launch failed before or after the model turn."""

    def __init__(self, message: str, *, before_model_turn: bool):
        super().__init__(message)
        self.before_model_turn = before_model_turn


class UnknownEffectError(BackendError):
    """The backend cannot prove whether a side effect happened."""


@dataclass(frozen=True)
class BackendHandle:
    idempotency_key: str
    backend_id: str


@dataclass(frozen=True)
class BackendObservation:
    idempotency_key: str
    state: str  # never-started | active | settled | ambiguous
    backend_id: str | None
    model_turn_started: bool
    result: str | None = None


class WorkerBackend(Protocol):
    """Minimal protocol consumed by the launch coordinator."""

    def launch(self, idempotency_key: str) -> BackendHandle:
        ...

    def reconcile(self, idempotency_key: str) -> BackendObservation:
        ...


@dataclass
class _Effect:
    state: str
    backend_id: str | None = None
    model_turn_started: bool = False
    result: str | None = None


class FakeBackend:
    """A deterministic backend whose effects survive coordinator restarts."""

    def __init__(self, scripts: Mapping[str, Iterable[str]] | None = None):
        self._scripts = {key: list(outcomes) for key, outcomes in (scripts or {}).items()}
        self._effects: dict[str, _Effect] = {}
        self._next_backend_id = 1
        self.launch_calls: list[str] = []

    def launch(self, idempotency_key: str) -> BackendHandle:
        self.launch_calls.append(idempotency_key)
        existing = self._effects.get(idempotency_key)
        if existing is not None:
            if existing.state == "ambiguous":
                raise UnknownEffectError(
                    f"effect {idempotency_key!r} remains ambiguous; reconcile before retrying"
                )
            if existing.backend_id is None:
                raise BackendError("effect has no backend identity")
            return BackendHandle(idempotency_key, existing.backend_id)

        outcome = self._scripts.get(idempotency_key, ["started"])
        kind = outcome.pop(0) if outcome else "started"
        if kind == "pre-turn-transient":
            raise TransientLaunchError("transient pre-turn launch failure", before_model_turn=True)
        if kind == "post-turn-transient":
            self._effects[idempotency_key] = _Effect(
                "ambiguous", model_turn_started=True
            )
            raise TransientLaunchError("post-turn launch result was lost", before_model_turn=False)
        if kind == "ambiguous":
            self._effects[idempotency_key] = _Effect("ambiguous")
            raise UnknownEffectError("backend lost the launch result")
        if kind not in {"started", "settled"}:
            raise BackendError(f"unknown fake launch script outcome {kind!r}")
        backend_id = f"fake-worker-{self._next_backend_id}"
        self._next_backend_id += 1
        effect = _Effect("active", backend_id=backend_id)
        if kind == "settled":
            effect.state = "settled"
            effect.result = "completed"
        self._effects[idempotency_key] = effect
        return BackendHandle(idempotency_key, backend_id)

    def begin_model_turn(self, idempotency_key: str) -> None:
        effect = self._require(idempotency_key)
        if effect.state != "active":
            raise BackendError(f"cannot begin a model turn from {effect.state!r}")
        effect.model_turn_started = True

    def settle(self, idempotency_key: str, result: str = "completed") -> None:
        effect = self._require(idempotency_key)
        if effect.state == "settled":
            if effect.result != result:
                raise BackendError("settlement contradicts the existing result")
            return
        if effect.state != "active":
            raise UnknownEffectError("cannot settle an ambiguous effect")
        effect.state = "settled"
        effect.result = result

    def reconcile(self, idempotency_key: str) -> BackendObservation:
        effect = self._effects.get(idempotency_key)
        if effect is None:
            return BackendObservation(idempotency_key, "never-started", None, False)
        return BackendObservation(
            idempotency_key,
            effect.state,
            effect.backend_id,
            effect.model_turn_started,
            effect.result,
        )

    def _require(self, idempotency_key: str) -> _Effect:
        try:
            return self._effects[idempotency_key]
        except KeyError as error:
            raise BackendError(f"unknown effect {idempotency_key!r}") from error


@dataclass(frozen=True)
class LaunchDecision:
    status: str  # started | ambiguous
    handle: BackendHandle | None
    retries: int
    observation: BackendObservation | None = None


class LaunchCoordinator:
    """Apply the one narrowly permitted pre-turn retry policy."""

    def __init__(self, backend: WorkerBackend):
        self.backend = backend

    def launch(self, idempotency_key: str) -> LaunchDecision:
        retries = 0
        while True:
            try:
                return LaunchDecision("started", self.backend.launch(idempotency_key), retries)
            except TransientLaunchError as error:
                if not error.before_model_turn or retries >= 1:
                    raise
                retries += 1
            except UnknownEffectError:
                observation = self.backend.reconcile(idempotency_key)
                return LaunchDecision("ambiguous", None, retries, observation)

    def recover(self, idempotency_key: str) -> BackendObservation:
        """Observe persisted backend identity without issuing a replacement launch."""

        return self.backend.reconcile(idempotency_key)
