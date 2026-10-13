-- Preserve attempt identity while separating planned page pauses from crash retries.
-- Bootstrap temporarily disables FK enforcement for this transactional table rebuild,
-- then checks every relationship before committing and restores enforcement.
CREATE TABLE jobs_new (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind='chat'),
    version INTEGER NOT NULL CHECK(version=1),
    profile_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','cancel_requested','interrupted','succeeded','failed','cancelled')),
    next_step INTEGER NOT NULL DEFAULT 1 CHECK(next_step BETWEEN 1 AND 5),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    deadline_at TEXT NOT NULL,
    progress TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(id,turn_id),
    FOREIGN KEY(turn_id,conversation_id) REFERENCES turns(id,conversation_id)
);
INSERT INTO jobs_new SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX jobs_by_status ON jobs(status,created_at);
CREATE INDEX jobs_by_conversation ON jobs(conversation_id,created_at);

CREATE TABLE job_steps_new (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 4),
    kind TEXT NOT NULL CHECK(kind IN ('chat','tool')),
    invocation_id TEXT UNIQUE REFERENCES tool_invocations(id),
    status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','interrupted','complete','failed','cancelled','outcome_unknown')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id,ordinal,kind)
);
INSERT INTO job_steps_new SELECT * FROM job_steps;
DROP TABLE job_steps;
ALTER TABLE job_steps_new RENAME TO job_steps;

CREATE TABLE page_pauses (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    segments INTEGER NOT NULL CHECK(segments >= 1)
);
