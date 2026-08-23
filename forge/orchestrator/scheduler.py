"""Deterministic bounded DAG scheduling over model-independent worker nodes."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class SchedulerError(ValueError):
    """A plan or scheduler operation violates orchestration policy."""


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROFILES = {"observe", "research", "modify-isolated", "review"}
_TERMINAL = {"completed", "partial", "blocked", "failed", "cancelled"}
_ACCEPTABLE_DEPENDENCY = {"completed", "verified", "integrated"}
_UNACCEPTABLE_DEPENDENCY = {
    "blocked",
    "failed",
    "cancelled",
    "verified-with-caveats",
    "refuted",
    "retained-isolated",
    "rejected",
}
_REVIEW_VERDICTS = {"verified", "verified-with-caveats", "refuted"}


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    worker_id: str
    dependencies: tuple[str, ...]
    permission_profile: str
    repository_id: str
    timeout_seconds: int

    @property
    def mutation(self) -> bool:
        return self.permission_profile == "modify-isolated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "workerId": self.worker_id,
            "dependencies": list(self.dependencies),
            "permissionProfile": self.permission_profile,
            "repositoryId": self.repository_id,
            "timeoutSeconds": self.timeout_seconds,
        }


@dataclass
class NodeState:
    spec: NodeSpec
    status: str = "queued"
    started_at: float | None = None
    evidence: list[str] = field(default_factory=list)
    verdict: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "status": self.status,
            "startedAt": self.started_at,
            "evidence": list(self.evidence),
            "verdict": self.verdict,
        }


def _required_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchedulerError(f"{path} must be a non-empty string")
    return value


def _node(value: NodeSpec | dict[str, Any]) -> NodeSpec:
    if isinstance(value, NodeSpec):
        value = value.to_dict()
    required = {
        "nodeId",
        "workerId",
        "dependencies",
        "permissionProfile",
        "repositoryId",
        "timeoutSeconds",
    }
    if set(value) != required:
        raise SchedulerError("node has unknown or missing fields")
    node_id = _required_string(value["nodeId"], "node.nodeId")
    if not _NODE_ID_RE.fullmatch(node_id):
        raise SchedulerError(f"node.nodeId must match {_NODE_ID_RE.pattern}")
    worker_id = _required_string(value["workerId"], "node.workerId")
    dependencies = value["dependencies"]
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
        raise SchedulerError("node.dependencies must be an array of non-empty strings")
    if len(set(dependencies)) != len(dependencies):
        raise SchedulerError(f"node {node_id!r} has duplicate dependencies")
    profile = value["permissionProfile"]
    if profile not in _PROFILES:
        raise SchedulerError(f"node.permissionProfile must be one of {sorted(_PROFILES)!r}")
    repository_id = _required_string(value["repositoryId"], "node.repositoryId")
    timeout = value["timeoutSeconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise SchedulerError("node.timeoutSeconds must be a positive integer")
    return NodeSpec(node_id, worker_id, tuple(dependencies), profile, repository_id, timeout)


def validate_plan(nodes: Iterable[NodeSpec | dict[str, Any]]) -> tuple[NodeSpec, ...]:
    """Validate IDs, references, and acyclicity before a scheduler is created."""

    normalized = tuple(_node(node) for node in nodes)
    by_id: dict[str, NodeSpec] = {}
    for node in normalized:
        if node.node_id in by_id:
            raise SchedulerError(f"duplicate node id {node.node_id!r}")
        by_id[node.node_id] = node
    for node in normalized:
        for dependency in node.dependencies:
            if dependency not in by_id:
                raise SchedulerError(f"node {node.node_id!r} references unknown dependency {dependency!r}")
            if dependency == node.node_id:
                raise SchedulerError(f"node {node.node_id!r} cannot depend on itself")
        if node.mutation and f"{node.node_id}.review" in by_id:
            raise SchedulerError(f"node {node.node_id!r} has a reserved review-node ID")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise SchedulerError(f"dependency cycle includes {node_id!r}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in by_id[node_id].dependencies:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(by_id):
        visit(node_id)
    return tuple(sorted(normalized, key=lambda node: node.node_id))


class Scheduler:
    """A deterministic in-memory scheduler whose state can be snapshotted/rebuilt."""

    def __init__(
        self,
        nodes: Iterable[NodeSpec | dict[str, Any]],
        *,
        max_concurrency: int = 3,
        clock: Callable[[], float] | None = None,
    ):
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 3:
            raise SchedulerError("max_concurrency must be between 1 and 3")
        self.max_concurrency = max_concurrency
        self._clock = clock or time.monotonic
        self._nodes = {node.node_id: NodeState(node) for node in validate_plan(nodes)}

    @property
    def nodes(self) -> tuple[NodeState, ...]:
        return tuple(self._nodes[node_id] for node_id in sorted(self._nodes))

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(node.snapshot() for node in self.nodes)

    def _refresh_blocked(self) -> None:
        for node in self.nodes:
            if node.status != "queued":
                continue
            dependencies = [self._nodes[dependency] for dependency in node.spec.dependencies]
            if any(
                dependency.status in _UNACCEPTABLE_DEPENDENCY
                or (dependency.status == "partial" and node.spec.permission_profile != "review")
                for dependency in dependencies
            ):
                node.status = "blocked"
                node.evidence.append("dependency did not reach an acceptable terminal state")

    def ready(self) -> tuple[NodeState, ...]:
        """Return only dependency-ready nodes, in stable node-ID order."""

        self._refresh_blocked()
        result = []
        for node in self.nodes:
            if node.status != "queued":
                continue
            if not all(
                self._nodes[dependency].status in _ACCEPTABLE_DEPENDENCY
                or (node.spec.permission_profile == "review" and self._nodes[dependency].status == "partial")
                for dependency in node.spec.dependencies
            ):
                continue
            result.append(node)
        return tuple(result)

    def _running_count(self) -> int:
        return sum(node.status == "running" for node in self._nodes.values())

    def _mutation_running(self, repository_id: str) -> bool:
        return any(
            node.status == "running" and node.spec.mutation and node.spec.repository_id == repository_id
            for node in self._nodes.values()
        )

    def start_ready(self, *, now: float | None = None) -> tuple[NodeState, ...]:
        """Reserve as many ready nodes as policy permits."""

        current = self._clock() if now is None else now
        started = []
        for node in self.ready():
            if self._running_count() >= self.max_concurrency:
                break
            if node.spec.mutation and self._mutation_running(node.spec.repository_id):
                continue
            node.status = "running"
            node.started_at = current
            started.append(node)
        return tuple(started)

    def complete(self, node_id: str, status: str, *, evidence: Iterable[str] = ()) -> NodeState:
        """Settle a running node; mutation completion inserts its review node."""

        node = self._get(node_id)
        if node.status != "running":
            raise SchedulerError(f"node {node_id!r} is not running")
        if node.spec.permission_profile == "review":
            if status not in _REVIEW_VERDICTS:
                raise SchedulerError("review nodes require a typed review verdict")
            node.verdict = status
        elif status not in _TERMINAL:
            raise SchedulerError(f"worker status must be one of {sorted(_TERMINAL)!r}")
        node.status = status
        node.evidence.extend(_required_string(item, "evidence") for item in evidence)
        if node.spec.mutation and status in {"completed", "partial"}:
            self._insert_review(node)
        return node

    def cancel(self, node_id: str, reason: str = "cancelled") -> NodeState:
        node = self._get(node_id)
        if node.status not in {"queued", "running"}:
            raise SchedulerError(f"node {node_id!r} cannot be cancelled from {node.status!r}")
        node.status = "cancelled"
        node.evidence.append(_required_string(reason, "reason"))
        return node

    def tick(self, *, now: float | None = None) -> tuple[NodeState, ...]:
        """Convert expired running workers to partial, retaining timeout evidence."""

        current = self._clock() if now is None else now
        expired = []
        for node in self.nodes:
            if node.status != "running" or node.started_at is None:
                continue
            if current - node.started_at < node.spec.timeout_seconds:
                continue
            node.status = "partial"
            node.evidence.append(f"timed out after {node.spec.timeout_seconds}s")
            if node.spec.mutation:
                self._insert_review(node)
            expired.append(node)
        return tuple(expired)

    def add_node(self, node: NodeSpec | dict[str, Any]) -> None:
        """Reject model/backend-created children; plans are immutable after start."""

        _node(node)
        raise SchedulerError("workers cannot create child nodes")

    def _insert_review(self, mutation: NodeState) -> None:
        review_id = f"{mutation.spec.node_id}.review"
        if review_id in self._nodes:
            return
        review = NodeSpec(
            node_id=review_id,
            worker_id=f"{mutation.spec.worker_id}.review",
            dependencies=(mutation.spec.node_id,),
            permission_profile="review",
            repository_id=mutation.spec.repository_id,
            timeout_seconds=300,
        )
        self._nodes[review_id] = NodeState(review)

    def _get(self, node_id: str) -> NodeState:
        try:
            return self._nodes[node_id]
        except KeyError as error:
            raise SchedulerError(f"unknown node {node_id!r}") from error
