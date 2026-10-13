# Large public pages (0.6.1)

After approval of an exact public URL, ordinary page reading saves one extracted
text snapshot in SQLite. It retains paragraph, heading and table-row boundaries,
visible endnotes outside `<main>` and inline TeX script text. Executable scripts,
navigation and hidden content are excluded. The saved text is available under
the existing tool details; opening it makes an authenticated local request and
displays literal text. It never loads the website again.

If the complete current exchange fits, Jarvis answers directly. Otherwise the
application uses the active runtime context size, the model's exact tokenizer,
required question, prompt framing and safety/output reserves to plan overlapping
source ranges. It processes them sequentially with the same resident model.
Internal calls have no tools and a separate output allowance (at most 512 tokens);
the configured final answer allowance is unchanged. Final fitting includes the
actual tool schemas, instructions, complete current exchange and final reserve.

Intermediate notes contain only exact source passages selected by the model.
Unsupported quotes and generated summaries cannot enter subsequent prompts.
The model selects passage IDs through a constrained JSON schema. The application
copies their original text itself, validates source substrings, and retains each
quote's source-range labels through merging; generated prose is never a note.
Hierarchical merges can select only previously verified passages. Selection and
merging can omit useful details: processing all ranges does not guarantee an
exhaustive or correct final answer. The UI reports coverage separately from
model prose and explicitly describes the notes as compressed evidence.

Snapshots and plans are immutable by identity. Model/runtime hash, context,
tokenizer/runtime configuration identity, question, instruction/schema/prompt
version and output budget identify a plan. A changed configuration makes a new
plan; it never reuses incompatible notes. Each finished note is committed under
the current job attempt. Explicit continuation reuses finished notes and the
snapshot, including after restart, without replaying the GET. Old completed
exchanges use the saved final notes when selected as history.

The current bounds are 256 KiB transferred bytes, 512 KiB decompressed HTML,
256 KiB extracted UTF-8 text, 10 seconds for download, 256 source ranges, four
merge levels, 8 KiB per saved note/digest and twelve internal generations per
execution segment. Work also pauses at a checkpoint after 240 seconds (a current
call may take longer, within its existing timeout). The user selects Continue to
start the next segment. Model-stage failures preserve checkpoints and require
explicit continuation; Cancel remains terminal. Ordinary crash retries retain
their three-attempt allowance; planned pauses do not consume that allowance.
Unfittable required input and source-size/plan limits fail explicitly.

Tool details show processed/total ranges and exact input/output token counts for
successfully saved notes. Failed/discarded model calls are not included in those
counts. The existing message statistics still count visible user/assistant text.
SQLite backups include snapshots, notes and progress automatically. Deleting a
chat removes them atomically through foreign keys. Migrations 014 and 015 preserve
existing rows; the job-table rebuild checks all relationships before commit and
restores enforcement even on failure.

This extends the existing approved HTML/plain-text fetch only. There is no link
crawling, search, new model, PDF reader, persistent retrieval index or background
fetch. The separately enabled offline JavaScript renderer retains its existing
bounded rendered-text/screenshot behavior; it does not gain full-page analysis.
