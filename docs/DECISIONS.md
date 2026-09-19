# Annie decisions

Record consequential product and technical choices, including why they were
made. Current behavior belongs in `../SPEC.md`; work status belongs in `TODO.md`.

## DEC-001: Shared documentation and agent instructions

- Date: 2026-09-19.
- Status: Adopted as the initial repository convention; revisable.
- Context: The repository needs a documentation convention shared by contributors and coding agents.
- Decision: Keep product scope in `SPEC.md`, actionable work in `docs/TODO.md`, exploratory ideas in `docs/BRAINSTORM.md`, and rationale here. Keep shared agent rules in `AGENTS.md`, with `CLAUDE.md` as a relative symlink.
- Reason: Each document has one purpose, and both agent entry points use the same maintained instructions.
- Tradeoff: Symlink support is needed in each checkout. For Windows contributors without it, replace the link with a regular `CLAUDE.md` containing `@AGENTS.md`.

## Future entries

Use the next `DEC-NNN` ID. Include date, status, context, decision, alternatives,
and consequences. If a decision changes, add a new entry and mark the older one
superseded with a link. No product or technology decisions have been recorded in
this log yet; describe the existing starter implementation in the README.
