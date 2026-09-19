# Annie

This is the repository scaffold for Annie. The documentation structure is
product- and stack-agnostic; requirements and implementation details can be
filled in as work takes shape.

## Project guide

| File | Purpose |
| --- | --- |
| [SPEC.md](SPEC.md) | Intended product behavior, scope, acceptance criteria, and unresolved requirements. |
| [AGENTS.md](AGENTS.md) | Shared instructions for coding agents working in this repository. |
| [CLAUDE.md](CLAUDE.md) | Relative symlink to `AGENTS.md`; edit the target file. |
| [docs/TODO.md](docs/TODO.md) | Prioritized, actionable work and completion conditions. |
| [docs/BRAINSTORM.md](docs/BRAINSTORM.md) | Uncommitted ideas, alternatives, and experiments. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Consequential decisions and their rationale. |
| [docs/hackmit-2026/NOTES.md](docs/hackmit-2026/NOTES.md) | Team notes, demo direction, ownership, hardware, and source history. |
| [docs/hackmit-2026/SPONSORS.md](docs/hackmit-2026/SPONSORS.md) | All supplied sponsor resources, credits, links, and unresolved details. |
| [backend/README.md](backend/README.md) | Setup and local development for the existing backend starter. |

## Development

The repository includes a Python 3.10+ FastAPI starter in `backend/`. It exposes
a welcome response at `/` and a health response at `/health`. Follow the
[backend setup and run instructions](backend/README.md) to develop it locally.

Automated test and lint commands are not configured yet. Document those commands
alongside the relevant component when they are added.

## Working flow

1. Explore possibilities in `docs/BRAINSTORM.md` when useful.
2. Define the chosen behavior and how to verify it in `SPEC.md`.
3. Record consequential choices and tradeoffs in `docs/DECISIONS.md`.
4. Break the agreed scope into small tasks in `docs/TODO.md`.
5. Implement and verify a complete user workflow; update affected documentation.

Ideas are proposals. Tasks track delivery. The specification defines intended
behavior. Keep each fact in its primary document and link to it elsewhere.

## Shared agent instructions

`AGENTS.md` is the canonical file. `CLAUDE.md` points to it using a relative
symlink, created from the repository root with `ln -s AGENTS.md CLAUDE.md`.
The link already exists; edit `AGENTS.md` directly.

[Codex discovers AGENTS.md automatically](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
[Claude Code supports the symlink](https://code.claude.com/docs/en/memory#share-one-file-with-other-coding-tools).
For a team using Windows without symlink support, use a regular `CLAUDE.md`
containing `@AGENTS.md` instead, so there is still only one copy of the rules.

Add architecture, deployment, or feature-specific documents when those topics
have enough substance to need their own files.
