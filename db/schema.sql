PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS meta_kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS meridian_nodes (
  id TEXT PRIMARY KEY,
  parent_id TEXT REFERENCES meridian_nodes(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  node_type TEXT NOT NULL CHECK (node_type IN ('menu', 'module', 'button')),
  sort_order INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_meridian_nodes_parent_sort
ON meridian_nodes(parent_id, sort_order);

CREATE TABLE IF NOT EXISTS meridian_details (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL REFERENCES meridian_nodes(id) ON DELETE CASCADE,
  basic_info TEXT NOT NULL DEFAULT '',
  input_preconditions TEXT NOT NULL DEFAULT '',
  exec_workflow TEXT NOT NULL DEFAULT '',
  design_pattern TEXT NOT NULL DEFAULT '',
  agent_collab TEXT NOT NULL DEFAULT '',
  system_observability TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(node_id)
);

CREATE TABLE IF NOT EXISTS meridian_feedback (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES meridian_nodes(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('feedback', 'need_clarification', 'pending_verify', 'accepted')),
  content TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_meridian_feedback_node_status
ON meridian_feedback(node_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS meridian_feedback_replies (
  id TEXT PRIMARY KEY,
  feedback_id TEXT NOT NULL REFERENCES meridian_feedback(id) ON DELETE CASCADE,
  reply_by TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_meridian_feedback_replies_feedback
ON meridian_feedback_replies(feedback_id, created_at DESC);

CREATE TABLE IF NOT EXISTS meridian_logs (
  id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES meridian_nodes(id) ON DELETE SET NULL,
  action TEXT NOT NULL,
  message TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_meridian_logs_node_time
ON meridian_logs(node_id, created_at DESC);

CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
  summary TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT 'system',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_task_runs_status_created
ON task_runs(status, created_at DESC);

INSERT INTO meta_kv(key, value)
VALUES ('schema_version', 'v1')
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
