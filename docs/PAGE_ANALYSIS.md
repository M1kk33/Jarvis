# Large public pages (0.6.2)

After approval of an exact public URL, ordinary page reading saves one extracted
text snapshot in SQLite. It retains paragraph, heading levels and table-row boundaries,
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

Each source range is split into up to four contiguous regions. A constrained
response must include a subject, factual summary and supporting passage ID for
EVERY region, preserving beginning, middle and end without requiring headings.
This works for continuous prose, lists and tables. Source headings and subtopics
are retained separately when available; fenced code comments are not headings.
Without an outline, the region subjects form the coverage inventory. The chunk
planner always advances the covered end and does not repeatedly read tiny tails.

Notes now contain generated summaries as well as exact evidence. The application
copies evidence from source IDs, verifies substrings and retains quote provenance
through merges. A merge receives ALL input summaries and retains the complete
subject inventory independently of which supporting quotes are selected. It may
not substitute invented quotations. Generated summaries can still be inaccurate
or omit details; reading every range is not a guarantee of semantic completeness.

Before committing a final answer based on analyzed notes, a bounded model call
checks whether the user requested a whole-page explanation and which subjects
or subtopics were missed. A focused question does not force a whole-page rewrite.
For every whole-page request, one constrained final pass requires a section
for every subject and a separate explanation for every listed subtopic, even if
the model's self-review claims nothing is missing. Missing
fields, invalid output or exhausted limits preserve the draft and checkpoints
instead of acknowledging a complete answer. This semantic check can itself make
mistakes; it improves coverage without promising error-free explanations.
The answer allowance remains unchanged. Both review and rewrite use the active
model's exact tokenizer, context limit and reserves. If needed, the rewrite omits
supporting quotes from its prompt, retaining every summary and subject; the
original evidence remains saved in SQLite. Unfittable input fails explicitly.
The rewrite also returns to the saved snapshot for short exact definition anchors
under known subject/subtopic titles; it does not rely solely on merged summaries
or download the page again.

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
source-analysis execution segment. Final coverage checking adds at most two
bounded generations within the existing overall job deadline. Work also pauses
at a checkpoint after 240 seconds (a current
call may take longer, within its existing timeout). The user selects Continue to
start the next segment. Model-stage failures preserve checkpoints and require
explicit continuation; Cancel remains terminal. Ordinary crash retries retain
their three-attempt allowance; planned pauses do not consume that allowance.
Unfittable required input and source-size/plan limits fail explicitly.

Tool details show processed/total ranges and exact input/output token counts for
successfully saved notes. Failed/discarded calls and final review/rewrite calls
are not included in those saved-note counts. The existing message statistics
still count visible user/assistant text, including a committed rewritten answer.
SQLite backups include snapshots, notes and progress automatically. Deleting a
chat removes them atomically through foreign keys. Migrations 014 and 015 preserve
existing rows; the job-table rebuild checks all relationships before commit and
restores enforcement even on failure.

This extends the existing approved HTML/plain-text fetch only. There is no link
crawling, search, new model, PDF reader, persistent retrieval index or background
fetch. The separately enabled offline JavaScript renderer retains its existing
bounded rendered-text/screenshot behavior; it does not gain full-page analysis.
