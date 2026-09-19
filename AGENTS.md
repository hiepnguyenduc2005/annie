# Working on Annie

## Orientation

- Read `README.md`, `SPEC.md`, and the relevant entries in `docs/TODO.md` before implementation.
- Follow the current user request. `SPEC.md` records intended behavior; unresolved fields are not requirements.
- Read `docs/DECISIONS.md` when a task touches an established product or technical choice.
- Treat `docs/BRAINSTORM.md` as proposals. Do not implement ideas merely because they appear there.

## Changes

- Keep changes focused on the requested outcome. Resolve routine implementation details using existing conventions.
- Ask about missing product decisions that materially change the result; continue independent work where possible.
- If implementation, the specification, and the request disagree, make the mismatch explicit and resolve it using the current request.
- Preserve unrelated work. Never put credentials, private user data, or machine-specific account configuration in tracked files.
- Record new or changed requirements in `SPEC.md`; mention material scope changes in the handoff.
- Update affected TODO entries and documentation with the implementation. Record consequential decisions with their rationale; avoid logging routine edits.
- Commit and push small, coherent milestones after appropriate verification. Push regularly as work progresses; use the requested account, preserve unrelated changes, and report any push failure. Never force-push without explicit direction.

## Verification and handoff

- Use the commands documented in `README.md` and component READMEs. From the root, run `PYTHONPATH=app_backend .venv/bin/python -m pytest app_backend/tests -q`, `.venv/bin/python contract/export_schemas.py --check`, and `node --check frontend/app.js` for the software demo. SDK/simulator checks have separate prerequisites.
- Verify changed behavior against its acceptance criteria, including relevant failure cases. Use checks appropriate to the change.
- Report what changed, what was verified, and any remaining blockers. Never claim an unrun check passed.
- Mark a task complete only when its stated completion condition is satisfied.
- For simulator changes, follow `simulation/README.md`; distinguish direct MuJoCo physics, DimOS integration, rendered-image inference, and actual robot runs. Keep model/dependency revisions and measured results reproducible.

## Engineering and collaboration

- Prefer the smallest complete user workflow. Keep transport, perception, policy, persistence, and presentation separate where their failure modes differ.
- Define typed contracts at boundaries. Validate external input; use stable IDs, explicit units and timestamps, bounded timeouts, and idempotent handling of retried events.
- Preserve backward compatibility when teammates depend on a contract. Change the producer, consumer, examples, and checks together; document breaking changes.
- Test meaningful behavior and failure paths, especially duplicate/stale events, disconnected services, authentication, and privacy boundaries. Mock paid APIs and hardware in routine tests.
- Keep credentials in ignored environment files, with names and safe defaults in `.env.example`. Do not log tokens, resident media, or raw provider responses.
- Distinguish simulated, queued, acknowledged, and executed behavior. Never describe a software stop as a hardware emergency stop, or a perception estimate as a diagnosis or calibrated measurement.
- Assign bounded work and distinct writable paths to parallel agents. Supply the contract and acceptance check; the integrating owner reviews their changes and runs the combined checks. Do not let workers commit another worker's unfinished files.
- Add dependencies only for a concrete need; prefer maintained libraries and reproducible installation. Avoid speculative abstractions and unrelated cleanup.

## Commits, reviews, and PRs

- Push verified, coherent milestones frequently. Inspect the staged diff and stage intended paths explicitly; never include another contributor's unfinished changes or `.env` contents.
- Fetch before pushing. If teammates advanced the branch, integrate their work and rerun affected checks; do not rewrite shared history.
- For PR-based work, use a focused branch and a title describing the resulting behavior. The description should explain the problem, change, validation, and material limitations; link related requirements.
- Keep PRs reviewable. Review correctness, failure behavior, data exposure, maintainability, and user-visible claims. Resolve actionable feedback with evidence and regression coverage when warranted.
- Follow the team's current delivery flow: this hackathon currently authorizes small direct pushes to `main`. Do not invent an approval gate, open a PR, or merge someone else's work merely because a template exists.

## Maintaining these instructions

- Edit `AGENTS.md` directly. `CLAUDE.md` is a relative symlink to this file.
- Keep these instructions short, portable, and specific to this repository. Link to detailed documentation instead of duplicating it.
- Keep personal model, account, and local tool preferences outside this shared repository.
