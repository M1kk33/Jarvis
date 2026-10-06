CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK (kind = 'chat'),
    version INTEGER NOT NULL CHECK (version = 1),
    profile_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'waiting_approval', 'cancel_requested',
        'interrupted', 'succeeded', 'failed', 'cancelled'
    )),
    next_step INTEGER NOT NULL DEFAULT 1 CHECK (next_step BETWEEN 1 AND 5),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 3),
    deadline_at TEXT NOT NULL,
    progress TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, turn_id),
    FOREIGN KEY (turn_id, conversation_id) REFERENCES turns(id, conversation_id)
);

CREATE INDEX jobs_by_status ON jobs(status, created_at);
CREATE INDEX jobs_by_conversation ON jobs(conversation_id, created_at);

CREATE TABLE job_steps (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 4),
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'tool')),
    invocation_id TEXT UNIQUE REFERENCES tool_invocations(id),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'waiting_approval', 'interrupted',
        'complete', 'failed', 'cancelled', 'outcome_unknown'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 3),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, ordinal, kind)
);

ALTER TABLE tool_invocations ADD COLUMN job_id TEXT REFERENCES jobs(id);
ALTER TABLE tool_invocations ADD COLUMN job_attempt INTEGER;
CREATE INDEX tool_invocations_by_job ON tool_invocations(job_id, ordinal);

ALTER TABLE tool_approvals ADD COLUMN job_id TEXT REFERENCES jobs(id);
ALTER TABLE tool_approvals ADD COLUMN job_attempt INTEGER;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    relative_path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('available', 'unavailable')),
    created_at TEXT NOT NULL
);
