# Annie — Home Companion

Front end + a mock backend for the aid-dog app. The front end works standalone
(hardcoded demo data) or connected live to the backend below — it detects
the backend automatically and falls back gracefully if it's not running,
so a crashed backend mid-demo never blanks the screen.

## Run it

The backend is a single-file, zero-dependency Swift server (Foundation + POSIX
sockets — no SwiftPM packages to fetch):

```bash
cd backend
swift main.swift            # or: swiftc -O main.swift -o annie-api && ./annie-api
```

Open **http://127.0.0.1:8000** — the backend serves the front end directly,
so there's nothing else to start and no CORS setup needed.

## What's where

- `frontend/annie-companion-app.html` — the whole UI (reminders, routine,
  Ask Annie, profile), vanilla HTML/CSS/JS, no build step.
- `backend/main.swift` — Swift HTTP server with three resources:
  - `reminders` — the scheduling/reminder list (`GET/POST /api/reminders`,
    `PATCH /api/reminders/{id}/toggle`)
  - `memory` — a spatiotemporal fact log: `{subject, relation, object, room,
    timestamp}` (`GET/POST /api/memory`) — this is the seam where real
    perception events from the dog (via DimOS Spatial Memory) get written in
  - `POST /api/ask` — answers questions by matching keywords against
    `reminders` and `memory`; swap the internals for a real DimOS Spatial
    Memory query later without changing the request/response shape

## Wiring in the real dog

Two integration points, both already shaped for it:

1. Whenever your perception pipeline / DimOS agent observes something
   (an object detection, a room change, a person event), `POST` it to
   `/api/memory` instead of writing to the in-memory list directly.
2. Swap the keyword matching in `/api/ask` for a real query against DimOS's
   Spatial Memory module — the request (`{question}`) and response
   (`{answer}`) shapes don't need to change, so the front end doesn't need
   to know the difference.

## Known simplifications (fine for a 24hr build, flag before anything real)

- Data is in-memory and resets on restart — swap for SQLite/a real DB if you
  need it to survive a restart during the demo.
- `/api/ask` uses simple keyword matching, not real language understanding.
- ElevenLabs isn't wired in yet — the front end currently uses the browser's
  built-in speech synthesis for "Read aloud" as a stand-in.//
//  README.swift
//  
//
//  Created by Sam Yarkoni on 9/19/26.
//

