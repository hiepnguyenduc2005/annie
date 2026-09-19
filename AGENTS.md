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

- Use the commands documented in `README.md` and the relevant component README. The current backend setup is in `backend/README.md`; automated test and lint commands are not configured yet.
- Verify changed behavior against its acceptance criteria, including relevant failure cases. Use checks appropriate to the change.
- Report what changed, what was verified, and any remaining blockers. Never claim an unrun check passed.
- Mark a task complete only when its stated completion condition is satisfied.

## Maintaining these instructions

- Edit `AGENTS.md` directly. `CLAUDE.md` is a relative symlink to this file.
- Keep these instructions short, portable, and specific to this repository. Link to detailed documentation instead of duplicating it.
- Keep personal model, account, and local tool preferences outside this shared repository.
