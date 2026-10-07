CREATE TABLE vision_inputs (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    profile_name TEXT NOT NULL,
    width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 1024),
    height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 1024)
);
