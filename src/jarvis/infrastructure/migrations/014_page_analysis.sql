CREATE TABLE page_snapshots (
    invocation_id TEXT PRIMARY KEY REFERENCES tool_invocations(id) ON DELETE CASCADE,
    text TEXT NOT NULL CHECK(length(CAST(text AS BLOB)) <= 262144),
    sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE page_analyses (
    id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES page_snapshots(invocation_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reading','merging','complete')),
    result TEXT,
    UNIQUE(invocation_id,turn_id,signature)
);
CREATE TABLE page_notes (
    analysis_id TEXT NOT NULL REFERENCES page_analyses(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    text TEXT NOT NULL CHECK(length(CAST(text AS BLOB)) <= 8192),
    input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens >= 0),
    PRIMARY KEY(analysis_id,stage,ordinal)
);
