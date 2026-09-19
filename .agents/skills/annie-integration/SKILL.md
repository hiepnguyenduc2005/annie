---
name: annie-integration
description: Develop or review Annie's robot, perception, voice, API, and app integrations against their shared contract. Use for cross-component payload or incident-flow changes in this repository, including mock-to-hardware transitions.
---

# Annie integration work

Read the relevant requirement in `SPEC.md` and boundary in `contract/README.md`.
Inspect producer and consumer before changing either; root `AGENTS.md` holds
general collaboration rules.

- Keep frame ID, capture timestamp, observer pose, and map ID attached to
  evidence. Robot coordinates are not measured person coordinates.
- Update typed models, exported schemas, producers, consumers, and regression
  checks together. Check whether new required fields/enums break teammate mocks.
- For incident changes, verify bed exemption, unknown/low-confidence input,
  stale/duplicate frames, correlated replies, timeout without new input, and
  one escalation per episode. `fall_confirmed` does not mean a diagnosis.
- Distinguish queued, consumed, and executed actions. A mock or HTTP 202 does
  not establish robot movement, audible playback, or notification delivery.
- Verify exactly which fields leave the local process before adding a cloud
  adapter. Treat crops, captions, and transcripts as potentially sensitive.
  Routine checks use synthetic data and mocked providers without paid calls.
- Keep provider failure out of incident policy. Subconscious agents are
  advisory and cannot move the dog or send alerts.
- Use documented test/run commands, regenerate schemas after model changes,
  and state whether verification used an in-process mock, network adapter,
  or physical hardware.
