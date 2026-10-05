CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    created_at TEXT NOT NULL
);

CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    position INTEGER NOT NULL CHECK (position > 0),
    submission_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed', 'interrupted')),
    created_at TEXT NOT NULL,
    error_code TEXT,
    UNIQUE (id, conversation_id),
    UNIQUE (conversation_id, position),
    UNIQUE (conversation_id, submission_id)
);

CREATE UNIQUE INDEX one_pending_turn_per_conversation
    ON turns(conversation_id) WHERE status = 'pending';

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    turn_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'complete'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id, conversation_id) REFERENCES turns(id, conversation_id),
    UNIQUE (conversation_id, position),
    UNIQUE (turn_id, role)
);

CREATE INDEX messages_by_conversation ON messages(conversation_id, position);
