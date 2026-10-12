ALTER TABLE turns ADD COLUMN regeneration_of TEXT REFERENCES turns(id);
ALTER TABLE turns ADD COLUMN regeneration_source TEXT REFERENCES turns(id);
CREATE INDEX turns_reply_versions ON turns(conversation_id, regeneration_of, position);
