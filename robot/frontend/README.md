# Annie family demo interface

Plain HTML, CSS, and JavaScript, served by `app_backend` at `/app/`. No frontend
build or package installation is needed. Run the root README commands.

The interface starts empty. Initialize the synthetic home, then exercise bed,
possible incident, reassurance, timeout, acknowledgment, queued speech, and
caption retrieval. Map pins describe robot observation positions. The software
scenario controls are not MuJoCo and do not trigger real hardware or messages.

API tokens are kept only in page memory and sent as bearer headers or the first
WebSocket message. The app refetches all events on reconnect/resync and renders
external text using textContent. Run `node --check robot/frontend/app.js` and verify
the primary flows in a real browser before claiming UI changes complete.
