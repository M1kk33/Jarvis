CREATE TABLE text_token_counts (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    output_sha256 TEXT,
    CHECK ((output_tokens IS NULL AND output_sha256 IS NULL)
        OR (output_tokens IS NOT NULL AND output_sha256 IS NOT NULL AND length(output_sha256) = 64))
);
