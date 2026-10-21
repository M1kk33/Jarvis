# Jarvis

Jarvis is a project to build a multitasking AI assistant that brings powerful models and practical tools together in one place.

The idea is to give the assistant a goal and let it organize the work: break it into tasks, set priorities, delegate each task to a suitable model, and use the tools needed to get it done. Jarvis should coordinate model access to limited hardware resources, deciding which work runs first and when to switch models.

The focus is on:

- **Multitasking:** managing several tasks and their dependencies.
- **Tools:** working with text, images, voice, files, and web sources.
- **Delegation:** choosing suitable models for different parts of a task.
- **Priorities:** scheduling work around urgency and available resources.

Jarvis is in active development. This describes the project vision; delegation and priority-based model orchestration are goals, rather than completed capabilities. The current implementation runs locally on Windows and executes one request at a time.
