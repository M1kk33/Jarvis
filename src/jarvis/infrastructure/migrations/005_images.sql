CREATE TABLE image_steps (
    invocation_id TEXT PRIMARY KEY REFERENCES tool_invocations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'complete', 'outcome_unknown', 'cancelled')),
    artifact_id TEXT UNIQUE REFERENCES artifacts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX image_steps_by_job ON image_steps(job_id, created_at);
