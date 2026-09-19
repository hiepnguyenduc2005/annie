# Annie brainstorm

Use this space for possibilities, alternatives, and experiments. Entries are
not commitments or implementation tasks.

## 2026-09-19: Home companion and family connection

Status: exploring, with a narrowed demo scope being developed.

The team proposed a robot dog connecting an elderly resident to family through
two-way communication, scene memory, and check-ins. [The HackMIT brief](hackmit-2026/NOTES.md)
preserves all supplied ideas, team assignments, resources, demo props, and
earlier alternatives. [The sponsor inventory](hackmit-2026/SPONSORS.md) preserves
the supplied credits and access details.

Later ideas include adaptive reminders and routine drift, multi-floor mapping,
care-center integration, social connection among elderly people, and optional
sensors. Follow the current scope in `SPEC.md` when implementation begins.

## 2026-09-19: Build the home scene around two useful questions

Working proposal: use one small bedroom/living-area scene for both incident
check-ins and memory. The physical scene, camera frames, and observations can
be reused rather than building separate demos.

| Question | Smallest experiment | What to measure |
| --- | --- | --- |
| Does Grandma need someone to check in? | Bed-rest versus lying on the floor, followed by reassurance, help, or silence. | Misses and false alerts per listed scenario, time to check-in, voice playback and app delivery. |
| Where did Annie last see the glasses? | Place a prop, observe it, move it, and ask before/after another observation. | Correct timestamp and cited frame; honest uncertainty when the new location was never observed. |
| Can family reach Grandma through Annie? | Queue a family message, play it, and return a correlated response. | Queue receipt versus real audible playback and response delivery. |

Prefer a guided patrol first: three fixed waypoints with an operator pause.
Free exploration adds navigation uncertainty before the perception loop works.
Use a clearly synthetic mannequin to develop the plumbing, then evaluate the
local VLM on rendered images separately. Scenario labels must never masquerade
as image-derived perception.

Open product choices: proactive periodic check-ins versus family-triggered
ones; which explicit reassurance closes a check-in; and whether family sees
a caption first with an optional released crop. A future spatial memory should
separate observer pose, estimated object position, and persistent object identity.
Each requires its own evidence; a timestamped caption does not prove all three.

The [simulation spec](../simulation/SPEC.md) records the working scenario matrix.

## Entry format

- Date and short title.
- Idea and the user problem it might address.
- Assumptions, open questions, and the smallest useful experiment.
- Outcome: exploring, chosen, deferred, or discarded; include the reason.

When an idea is chosen, put the resulting requirement in `../SPEC.md`, record
consequential rationale in `DECISIONS.md`, and add executable work to `TODO.md`.
Leave a short link here instead of maintaining duplicate requirements.
