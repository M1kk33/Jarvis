CREATE TABLE retrieval_roots (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(id),
    scope_key TEXT NOT NULL CHECK (scope_key = coalesce(conversation_id, '')),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (path, scope_key)
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4096),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE retrieval_documents (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES retrieval_roots(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'stale')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (root_id, path)
);

CREATE VIRTUAL TABLE retrieval_fts USING fts5(
    kind UNINDEXED,
    source_id UNINDEXED,
    location UNINDEXED,
    source_hash UNINDEXED,
    conversation_id UNINDEXED,
    text,
    tokenize='unicode61'
);
