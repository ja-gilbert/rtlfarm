-- Migration 0001: the whole schema.
--
-- Migration files contain no BEGIN or COMMIT; the runner wraps each file in
-- one BEGIN IMMEDIATE ... COMMIT, so a failure partway leaves neither the
-- schema changes nor the schema_migrations row. Every CHECK and table
-- constraint lives here because SQLite cannot add one later without
-- rebuilding the table; the indexes live here because the claim's query
-- plan and the one-COMMITTED-attempt guarantee are part of the schema.
--
-- Timestamps are INTEGER epoch milliseconds from the control plane's clock.
-- The schema is the state machine: the CHECKs on tasks make a forgotten
-- lease-clearing SET list fail at the first test instead of in production.

CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, design_name TEXT NOT NULL,
  submission_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, pipeline_json TEXT NOT NULL,
  selection_json TEXT NOT NULL, toolchain_digest TEXT NOT NULL,
  priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 9),
  no_cache INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT UNIQUE,
  label TEXT, rerun_of TEXT,                         -- free-form label; the job this one reruns
  state TEXT NOT NULL CHECK(state IN ('SUBMITTED','RUNNING','SUCCEEDED','FAILED','CANCELED')),
  dirty INTEGER NOT NULL DEFAULT 1, n_tasks INTEGER NOT NULL,
  n_terminal INTEGER NOT NULL DEFAULT 0,   -- tasks in a terminal state
  n_failed INTEGER NOT NULL DEFAULT 0,     -- affects_verdict=1 tasks in FAILED, TIMED_OUT, INFRA_FAILED
  n_cached INTEGER NOT NULL DEFAULT 0,     -- from_cache=1
  created_at_ms INTEGER NOT NULL, started_at_ms INTEGER, finished_at_ms INTEGER);
CREATE INDEX jobs_state_created ON jobs(state, created_at_ms);
CREATE INDEX jobs_dirty ON jobs(dirty) WHERE dirty=1;

CREATE TABLE job_inputs (job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  path TEXT NOT NULL, role TEXT NOT NULL, ordinal INTEGER NOT NULL,   -- compilation order
  sha256 TEXT NOT NULL REFERENCES blobs(sha256), size INTEGER NOT NULL, PRIMARY KEY (job_id, path));
CREATE INDEX job_inputs_sha ON job_inputs(sha256);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  stage_kind TEXT NOT NULL CHECK(stage_kind IN ('lint','compile','simulate','coverage','compile_wave','simulate_wave')),
  target TEXT NOT NULL, seed INTEGER NOT NULL,
  tool TEXT NOT NULL, params_json TEXT NOT NULL, timeout_s INTEGER NOT NULL,
  cacheable INTEGER NOT NULL, bypass_cache INTEGER NOT NULL DEFAULT 0,
  affects_verdict INTEGER NOT NULL DEFAULT 1, toolchain_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','READY','LEASED','RUNNING',
    'SUCCEEDED','FAILED','TIMED_OUT','INFRA_FAILED','SKIPPED','CANCELED')),
  priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 9),
  ready_at_ms INTEGER, requeued_at_ms INTEGER, not_before_ms INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
  infra_failures INTEGER NOT NULL DEFAULT 0, timeout_failures INTEGER NOT NULL DEFAULT 0,
  tool_error_failures INTEGER NOT NULL DEFAULT 0,
  max_infra INTEGER NOT NULL, max_timeout INTEGER NOT NULL, max_tool_error INTEGER NOT NULL,
  leased_by TEXT, lease_attempt_id TEXT, lease_expires_at_ms INTEGER, leased_at_ms INTEGER,
  started_at_ms INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0,
  cache_key TEXT, from_cache INTEGER NOT NULL DEFAULT 0, cache_hit_from TEXT,
  committed_attempt_id TEXT, exit_class TEXT, result_json TEXT,
  finished_at_ms INTEGER, duration_ms INTEGER, skip_reason TEXT
    CHECK(skip_reason IS NULL OR skip_reason IN ('upstream_failed','not_triggered')),
  CHECK ((state IN ('LEASED','RUNNING')) = (lease_attempt_id IS NOT NULL)),
  CHECK (state <> 'READY' OR ready_at_ms IS NOT NULL),
  CHECK (toolchain_digest <> 'any'));
CREATE INDEX tasks_ready_queue ON tasks(state, toolchain_digest, priority DESC, ready_at_ms, task_id, not_before_ms);  -- covering for the claim
CREATE INDEX tasks_job ON tasks(job_id, state);

CREATE TABLE task_deps (task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('success','failure')), PRIMARY KEY (task_id, depends_on_task_id));
CREATE INDEX task_deps_upstream ON task_deps(depends_on_task_id);   -- readiness skip statement: from a terminal upstream to its dependents

CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL, worker_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','COMMITTED','REPORTED','EXPIRED','ABORTED')),
  had_started INTEGER NOT NULL DEFAULT 0, leased_at_ms INTEGER NOT NULL, started_at_ms INTEGER,
  finished_at_ms INTEGER, expire_reason TEXT, outcome TEXT, exit_class TEXT, infra_reason TEXT,
  result_json TEXT, tool_env_json TEXT, reject_reason TEXT, rejected_at_ms INTEGER);
CREATE UNIQUE INDEX attempts_one_committed ON attempts(task_id) WHERE state='COMMITTED';
CREATE INDEX attempts_task ON attempts(task_id, attempt);
CREATE INDEX attempts_worker ON attempts(worker_id, state);   -- GET /v1/workers: current attempts per worker

CREATE TABLE task_events (task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, at_ms INTEGER NOT NULL, actor TEXT NOT NULL, from_state TEXT,
  to_state TEXT NOT NULL, attempt INTEGER, detail_json TEXT, PRIMARY KEY (task_id, seq));

CREATE TABLE workers (worker_id TEXT PRIMARY KEY, hostname TEXT NOT NULL,
  toolchain_digest TEXT NOT NULL, tools_json TEXT NOT NULL, slots INTEGER NOT NULL,
  version TEXT NOT NULL, registered_at_ms INTEGER NOT NULL, last_heartbeat_at_ms INTEGER,
  slots_free INTEGER, state TEXT NOT NULL CHECK(state IN ('ACTIVE','DEAD')));

CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, created_at_ms INTEGER NOT NULL);

CREATE TABLE task_artifacts (task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
  size INTEGER NOT NULL, PRIMARY KEY (task_id, name));
CREATE INDEX task_artifacts_sha ON task_artifacts(sha256);

CREATE TABLE cache_entries (cache_key TEXT PRIMARY KEY, stage_kind TEXT NOT NULL, tool TEXT NOT NULL,
  design_name TEXT NOT NULL, toolchain_digest TEXT NOT NULL, exit_class TEXT NOT NULL,
  result_json TEXT NOT NULL, origin_attempt_id TEXT NOT NULL,
  key_parts_json TEXT NOT NULL,            -- the key's components, kept so a hit or miss can be explained
  created_at_ms INTEGER NOT NULL, last_hit_at_ms INTEGER, hits INTEGER NOT NULL DEFAULT 0);
CREATE INDEX cache_entries_design ON cache_entries(design_name, stage_kind);

CREATE TABLE cache_artifacts (cache_key TEXT NOT NULL REFERENCES cache_entries(cache_key) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
  size INTEGER NOT NULL, PRIMARY KEY (cache_key, name));
CREATE INDEX cache_artifacts_sha ON cache_artifacts(sha256);

CREATE TABLE cache_disagreements (cache_key TEXT NOT NULL, attempt_id TEXT NOT NULL, task_id TEXT NOT NULL,
  at_ms INTEGER NOT NULL, detail_json TEXT NOT NULL, PRIMARY KEY (cache_key, attempt_id));   -- an attempt's result disagreed with the cached one
