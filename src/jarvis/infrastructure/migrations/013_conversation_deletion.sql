-- Keep only identity for committed deletion retries; no conversation content.
CREATE TABLE deleted_conversations (
    id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);

-- Filesystem work follows the database commit and can resume after a crash.
CREATE TABLE deleted_artifact_files (
    relative_path TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);
