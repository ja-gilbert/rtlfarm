"""The durable-state invariant checker.

The checker reads one snapshot of the database and lists every way the
recorded state contradicts the state machines. It is run after every fault
scenario in the test suite and on demand from the CLI, so a mechanism that
corrupts state fails a test by name instead of surfacing later as a wrong
result. The missing numbers (job aggregates, readiness, artifact files) need
the scheduler and land with it.

Invariants checked here:

1. At most one COMMITTED attempt per task. The partial unique index enforces
   this too; it is asserted here so the checker is meaningful on its own.
2. Every LEASED or RUNNING task has exactly one ACTIVE attempt, the one named
   by its lease, and every ACTIVE attempt belongs to a LEASED or RUNNING task
   whose lease names it.
3. No terminal task has a lease or start column set.
8. task_events.seq is contiguous from 1 per task, and the last event's
   to_state equals the task's state.
9. The dependency graph of every job is acyclic.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from rtlfarm.clock import Clock

TERMINAL_STATES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "TIMED_OUT", "INFRA_FAILED", "SKIPPED", "CANCELED"}
)


@dataclass(frozen=True)
class Violation:
    """One contradiction between the recorded state and an invariant."""

    invariant: int
    message: str

    def __str__(self) -> str:
        return f"invariant {self.invariant}: {self.message}"


class InvariantViolation(AssertionError):
    """The database contradicts at least one invariant; ``violations`` lists them."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__("\n".join(str(v) for v in violations))


def check_invariants(conn: sqlite3.Connection) -> list[Violation]:
    """Return every violation found in one snapshot of ``conn``'s database.

    The queries run inside one explicit ``BEGIN`` … ``COMMIT`` so they all see
    the same committed state; a read-only connection is enough.
    """
    conn.execute("BEGIN")
    try:
        found: list[Violation] = []
        found.extend(_one_committed_attempt_per_task(conn))
        found.extend(_leases_and_active_attempts_match(conn))
        found.extend(_terminal_tasks_hold_no_lease(conn))
        found.extend(_events_are_contiguous_and_current(conn))
        found.extend(_dependencies_are_acyclic(conn))
    finally:
        conn.execute("COMMIT")
    return found


def assert_invariants(conn: sqlite3.Connection, clock: Clock) -> None:
    """Raise ``InvariantViolation`` listing every violation, or return quietly.

    ``clock`` is unused so far; the time-based invariants will need it.
    """
    violations = check_invariants(conn)
    if violations:
        raise InvariantViolation(violations)


def _one_committed_attempt_per_task(conn: sqlite3.Connection) -> list[Violation]:
    rows = conn.execute(
        "SELECT task_id, COUNT(*) FROM attempts WHERE state = 'COMMITTED' "
        "GROUP BY task_id HAVING COUNT(*) > 1"
    ).fetchall()
    return [
        Violation(1, f"task {task_id} has {n} COMMITTED attempts")
        for task_id, n in rows
    ]


def _leases_and_active_attempts_match(conn: sqlite3.Connection) -> list[Violation]:
    found: list[Violation] = []
    leased = conn.execute(
        "SELECT task_id, state, lease_attempt_id FROM tasks "
        "WHERE state IN ('LEASED', 'RUNNING')"
    ).fetchall()
    for task_id, state, lease_attempt_id in leased:
        active = conn.execute(
            "SELECT attempt_id FROM attempts WHERE task_id = ? AND state = 'ACTIVE'",
            (task_id,),
        ).fetchall()
        ids = sorted(str(row[0]) for row in active)
        if ids != [lease_attempt_id]:
            found.append(
                Violation(
                    2,
                    f"task {task_id} is {state} with lease {lease_attempt_id} "
                    f"but its ACTIVE attempts are {ids}",
                )
            )
    stray = conn.execute(
        "SELECT a.attempt_id, a.task_id, t.state, t.lease_attempt_id "
        "FROM attempts a JOIN tasks t ON t.task_id = a.task_id "
        "WHERE a.state = 'ACTIVE' AND (t.state NOT IN ('LEASED', 'RUNNING') "
        "OR t.lease_attempt_id IS NOT a.attempt_id)"
    ).fetchall()
    for attempt_id, task_id, state, lease_attempt_id in stray:
        found.append(
            Violation(
                2,
                f"attempt {attempt_id} is ACTIVE but task {task_id} is {state} "
                f"with lease {lease_attempt_id}",
            )
        )
    return found


def _terminal_tasks_hold_no_lease(conn: sqlite3.Connection) -> list[Violation]:
    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    rows = conn.execute(
        "SELECT task_id, state FROM tasks "
        f"WHERE state IN ({placeholders}) AND (leased_by IS NOT NULL "
        "OR lease_attempt_id IS NOT NULL OR lease_expires_at_ms IS NOT NULL "
        "OR leased_at_ms IS NOT NULL OR started_at_ms IS NOT NULL)",
        tuple(sorted(TERMINAL_STATES)),
    ).fetchall()
    return [
        Violation(
            3, f"task {task_id} is {state} but still holds lease or start columns"
        )
        for task_id, state in rows
    ]


def _events_are_contiguous_and_current(conn: sqlite3.Connection) -> list[Violation]:
    found: list[Violation] = []
    rows = conn.execute(
        "SELECT task_id, seq, to_state FROM task_events ORDER BY task_id, seq"
    ).fetchall()
    per_task: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for task_id, seq, to_state in rows:
        per_task[str(task_id)].append((int(seq), str(to_state)))
    states = dict(conn.execute("SELECT task_id, state FROM tasks").fetchall())
    for task_id, events in per_task.items():
        seqs = [seq for seq, _ in events]
        if seqs != list(range(1, len(seqs) + 1)):
            found.append(Violation(8, f"task {task_id} event seq is {seqs}"))
        last_state = events[-1][1]
        if states.get(task_id) != last_state:
            found.append(
                Violation(
                    8,
                    f"task {task_id} is {states.get(task_id)} but its last event "
                    f"says {last_state}",
                )
            )
    return found


def _dependencies_are_acyclic(conn: sqlite3.Connection) -> list[Violation]:
    found: list[Violation] = []
    rows = conn.execute(
        "SELECT t.job_id, d.task_id, d.depends_on_task_id "
        "FROM task_deps d JOIN tasks t ON t.task_id = d.task_id"
    ).fetchall()
    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for job_id, task_id, upstream in rows:
        edges[str(job_id)].append((str(upstream), str(task_id)))
    for job_id, job_edges in edges.items():
        if _has_cycle(job_edges):
            found.append(Violation(9, f"job {job_id} has a dependency cycle"))
    return found


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    """Kahn's algorithm: a graph is acyclic iff every node can be removed."""
    indegree: dict[str, int] = defaultdict(int)
    downstream: dict[str, list[str]] = defaultdict(list)
    for upstream, task in edges:
        indegree.setdefault(upstream, 0)
        indegree[task] += 1
        downstream[upstream].append(task)
    ready = [node for node, n in indegree.items() if n == 0]
    removed = 0
    while ready:
        node = ready.pop()
        removed += 1
        for nxt in downstream[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    return removed != len(indegree)
