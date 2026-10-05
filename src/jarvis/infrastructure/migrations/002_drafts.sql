CREATE TABLE turn_drafts (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'interrupted')),
    checkpointed_at TEXT NOT NULL
);
