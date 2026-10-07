# M15 local retrieval

All commands use the same private `--data-dir`. Memories and document grants are global by default; add `--conversation-id ID` when creating either to keep it within one conversation. The user creates, edits, and deletes memories explicitly:

```powershell
uv run python -m jarvis --data-dir E:\JarvisData memory-add "The workshop meets on Friday"
uv run python -m jarvis --data-dir E:\JarvisData memory-list
uv run python -m jarvis --data-dir E:\JarvisData memory-edit MEMORY_ID "The workshop meets on Saturday"
uv run python -m jarvis --data-dir E:\JarvisData memory-delete MEMORY_ID
```

Grant one directory, then import individual UTF-8 `.txt` or `.md` files by absolute path. Granting a root does not crawl or index it. Each file is limited to 256 KiB; only bounded text chunks enter the FTS5 index.

```powershell
uv run python -m jarvis --data-dir E:\JarvisData retrieval-root-add E:\Notes
uv run python -m jarvis --data-dir E:\JarvisData document-import ROOT_ID E:\Notes\guide.md
uv run python -m jarvis --data-dir E:\JarvisData document-list
uv run python -m jarvis --data-dir E:\JarvisData retrieve CONVERSATION_ID "What does the guide say about setup?"
```

The search result includes a source ID, document path, and line range. Chat may include up to three source-labelled excerpts within its context budget. It checks an indexed file's hash against the current file before use; an edited, missing, or inaccessible file becomes stale and must be reindexed explicitly. `document-reindex DOCUMENT_ID` replaces its chunks atomically. `document-delete DOCUMENT_ID` removes a source. `retrieval-root-revoke ROOT_ID` immediately removes every indexed chunk under that root; regranting does not restore them without explicit reindexing.

The model sees excerpts as untrusted data. They cannot grant tool permissions or turn into system instructions. Search and indexing use SQLite and checked local file reads; no embedding model, filesystem watcher, background crawl, or network service is involved. Use `backup` to include memories and the index in a verified SQLite snapshot. After restoring on another machine, source files must still be present at their recorded paths or their chunks will be invalidated on search.
