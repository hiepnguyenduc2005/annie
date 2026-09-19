# Development workflow and skills

Official guidance and availability checked 2026-09-19 (September 2026).

`AGENTS.md` holds project-wide engineering and PR expectations. The focused
[Annie integration skill](../.agents/skills/annie-integration/SKILL.md) records
frame/pose, incident, adapter, and privacy invariants. Update it from actual
integration experience; keep generic engineering advice in `AGENTS.md`.

OpenAI skills installed in the current developer environment:

| Skill | Use when |
| --- | --- |
| `playwright` | Exercising and visually checking the local app in a browser. |
| `security-best-practices` | Applying requested FastAPI and browser security guidance. |
| `gh-fix-ci` | Diagnosing a failing GitHub Actions check. |
| `gh-address-comments` | Resolving actionable PR review comments. |

Personal installations are not vendored or guaranteed present on teammates'
machines. Existing OpenAI Docs, skill-creator, skill-installer, and cost-aware
delegation guidance also supported setup. Load skills when their workflow
applies; do not rewrite personal model/account configuration.

Sources: [OpenAI skill guidance](https://learn.chatgpt.com/docs/build-skills),
[curated skills](https://github.com/openai/skills/tree/main/skills/.curated),
and [project instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
This records guidance available on the check date, not future September releases.

Parallel contributors own distinct paths and code against the contract. The
integrating owner reviews changes and runs combined checks before pushing.
Source archives in `docs/hackmit-2026/sources/` retain original formatting;
lint authored documents separately from those unmodified attachments.
