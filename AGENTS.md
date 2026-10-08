# Jarvis implementation rules

- Work only on the assigned task. M1–M15 in `ARCHITECTURE.md` are implemented. The current user-authorized task adds a lightweight dedicated Windows application window, console-free shortcuts, graceful start/stop, and a visual refresh of the existing interface. Do not add models/tools or expand inference scope. Persistent retrieval remains explicit; do not add automatic life logging, speculative memory extraction, vector search, embeddings, knowledge graphs, or background filesystem crawling.
- Keep the direction `interfaces -> application -> domain`; infrastructure implements domain contracts, and bootstrap alone wires concrete classes. Core imports must need no GPU, model, network, or inference package.
- SQLite owns conversation and turn state. Submission and final reply each commit atomically with their turn transition. One database thread owns the connection; cancellation of an await requires outcome verification by stable identity. Check expected state before completion and acknowledge only committed results.
- Preserve incomplete and failed turns. A reused submission ID must not call the provider again or create messages; changed text with the same ID is an error.
- Select complete history groups within the context budget; retain required instructions/current input and fail explicitly if they do not fit. Stream with bounded output, one terminal result, and separate draft checkpoints. Logs contain metadata only.
- One heavy runtime may be resident and one request active. A warm idle worker keeps the device lock. Check cold peak or warm additional headroom against reserves; unavailable telemetry never becomes an invented number.
- Serialize runtime transitions. Readiness, exit, idle, and request events use generation/request identity; stale callbacks are inert. Caller cancellation cannot abandon load or cleanup. Failed or unverified tree exit keeps the slot unavailable and its device ownership held.
- Assign Windows children to a kill-on-close Job Object at creation, with no breakaway. Verify the full child tree before releasing ownership; never kill by executable name.
- Never reset or delete real user data to fix a migration. Test migrations and crash recovery with temporary SQLite databases.
- Keep the fake provider deterministic and interfaces thin. Add a module or dependency only for behavior implemented in the assigned task.
- Run the offline tests and verify the assigned task's acceptance criteria before reporting changes. Report any unresolved architecture conflict rather than silently bypassing it.
