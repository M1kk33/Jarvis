CREATE TABLE tool_invocations (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 4),
    model_call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    arguments_json TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'proposed', 'waiting_approval', 'intent', 'complete',
        'denied', 'cancelled', 'outcome_unknown'
    )),
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'approval', 'deny')),
    result_json TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (turn_id, conversation_id) REFERENCES turns(id, conversation_id),
    UNIQUE (turn_id, ordinal),
    UNIQUE (turn_id, model_call_id)
);

CREATE INDEX tool_invocations_by_turn ON tool_invocations(turn_id, ordinal);

CREATE TABLE tool_approvals (
    id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL UNIQUE REFERENCES tool_invocations(id),
    turn_id TEXT NOT NULL REFERENCES turns(id),
    arguments_sha256 TEXT NOT NULL,
    tool_version INTEGER NOT NULL,
    target_identity TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    session_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('issued', 'consumed', 'invalidated')),
    issued_at TEXT NOT NULL,
    consumed_at TEXT
);
