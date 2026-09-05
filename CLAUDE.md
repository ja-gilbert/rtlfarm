# Engineering conventions

rtlfarm is a small hardware regression farm: one control plane (FastAPI,
SQLite), pull-based workers, leases, a fenced idempotent commit, and a
content-addressed cache. Read `README.md` and `ARCHITECTURE.md` (once it
exists) before changing anything.

## Authorship of AI-assisted diffs

- The maintainer authors the core seams personally: the schema and state
  machines, the claim transaction, lease expiry and heartbeat renewal, the
  commit guard, worker fencing, cache-key composition, the invariant checker,
  the critical fault-injection tests, and every ADR. An AI assistant must not
  write or rewrite those files; it may explain, suggest tests, and review.
- Everything else (route plumbing, CLI, configuration, models, repository
  helpers, adapters, Compose, CI, fixtures, ordinary tests, documentation
  scaffolding) may be AI-implemented in pair mode. Every diff is reviewed by
  the maintainer before it is committed.
- The maintainer makes every git commit. An assistant never runs
  `git commit`, `git push`, `git tag`, or creates branches.
- No AI-authored test may be the sole evidence for a headline reliability
  claim in the README.

## Hard rules

- **No model in any code path.** Nothing at runtime, in tests, or in CI calls
  an LLM or depends on one.
- **Timing constants are configuration.** Lease TTLs, heartbeat periods,
  grace periods, backoffs, poll ceilings: all live in `rtlfarm.toml` /
  `RTLFARM_*` and are validated by `validate_timing()`. Never a literal number
  of seconds in code.
- **The control plane's clock is injected** (`rtlfarm.clock.Clock`). Never
  call `time.time()` in scheduler or lease code; tests drive a `DrivenClock`.
- **No secrets, no private documents.** `docs/private/`, `.env`, the database
  and blobs are gitignored; CI fails if `docs/private/` or `.env` is tracked.
- **Fault hooks are inert.** `hooks.point("name")` does nothing unless
  `RTLFARM_TEST_HOOKS=1`; no shipped Compose file sets it.

## Workflow

- Test first: the test file exists and fails before the implementation lands.
- `uv` manages the environment (`uv sync --frozen`); `uv.lock` is committed.
- Before claiming anything works: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy --strict src tests`,
  `uv run pytest`.
- Tests are tiered by directory: `tests/unit` (pure, < 30 s), `tests/fast`
  (in-process control plane, driven clock), `tests/process` (real
  subprocesses and signals), `tests/e2e` (Compose).
- Python 3.12, `from __future__ import annotations`, frozen dataclasses for
  configuration, `mypy --strict` clean, ruff-formatted at 88 columns.
- Prefer the plain, explainable implementation over the clever one; the
  maintainer must be able to explain every line in the repository.
