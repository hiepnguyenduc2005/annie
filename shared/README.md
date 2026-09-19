# Shared protocol references

The app-facing contract now permits typed status, map, perception, incident,
voice-transcript, and command data. See [the contract](../robot/contract/README.md)
and [exported schemas](../robot/contract/schemas.json). Typed producer/consumer
validation is implemented in `robot/app_backend/app/models.py`; the exporter checks
that published schemas match. A robot adapter can generate or vendor validators
from this versioned artifact without importing the app service at runtime.

`messages.py` retains the earlier status-only `RobotSignal` (version 1) for
compatibility with simple status producers. It is not a validator for rich
v0.1 payloads and must not be mistaken for the entire protocol.

No raw frame stream or arbitrary provider response is added implicitly.
Actual robot transport, device authentication, routing, and execution receipts
remain integration work.
