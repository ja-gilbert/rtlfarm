"""SQL shared between the control plane and the tests that check its query plans.

The ready queue is the set of tasks a worker may claim. Its ``SELECT`` is kept
here, so the claim and the query-plan test use the same statement: the test
proves that ``tasks_ready_queue`` serves it as a covering index with no
temporary sort, and the claim relies on that plan.
"""

from __future__ import annotations

#: The head of the ready queue for one toolchain digest, in claim order:
#: strict priority, then first ready, then task id as a deterministic tiebreak.
#: Parameters: ``:now`` (epoch ms) and ``:digest`` (the worker's toolchain).
READY_QUEUE_SELECT = """
SELECT task_id FROM tasks
WHERE state = 'READY' AND not_before_ms <= :now AND toolchain_digest = :digest
ORDER BY priority DESC, ready_at_ms, task_id
LIMIT 1
"""
