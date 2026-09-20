# Annie Swift frontend — v2 family API

SwiftUI app for iPhone and Mac. The four tabs use the MongoDB-backed API in
`app_backend/`. There is no local fake conversation or successful offline write.
Loaded records stay visible during a connection failure, with an error and retry.

## Start

1. Run app_backend using its README. Verify `/ready` succeeds.
2. Open `app_frontend/Annie.xcodeproj` in Xcode (the Swift package is the Mac app).
3. Select the Annie target and your signing Team. Personal settings belong in
   ignored `app_frontend/Local.xcconfig`:

   ```ini
   DEVELOPMENT_TEAM = YOUR_TEAM_ID
   PRODUCT_BUNDLE_IDENTIFIER = com.yourname.annie
   ANNIE_API_URL = http:/$()/YOUR-MAC-LAN-IP:8000
   ```

   Keep `$()` exactly as shown; it prevents `//` becoming an xcconfig comment.
   There is no API token. The simulator defaults to localhost when no local
   override is present. A physical phone needs the reachable Mac IP.
4. Build/run on the iPhone and allow Local Network access. Choose a family profile
   loaded from the server. Seed users are Zach (2) and Ellis (3), both Jeanine (1).
   Old string-ID profiles require selection again; old MongoDB conversations are
   not removed by this device-profile change.
5. The app connects automatically. Profile > Connect retries. Rebuild after
   changing the URL. The phone and Mac must have a reachable network connection.

Mac development uses `ANNIE_API_URL=http://127.0.0.1:8000 swift run AnnieApp`
from this directory. Environment variables override the bundled URL.

## Screens and synchronization

- **Reminders:** household-wide list, `daily_time` in the resident's timezone,
  description, and latest note. Completion is read-only and comes from today's
  robot reports. Synthetic seed reports are explicitly marked as demo examples.
  An added reminder appears for every family member; errors do not fabricate saves.
- **Ask Annie:** today's conversation only, private to the selected app user.
  Every message uses POST /api/messages. Display app-user, robot, and resident
  entries, plus queued/accepted/completed/failed status. There is no old-day picker;
  earlier conversations remain in MongoDB. The backend determines today in the
  resident's timezone; pagination pins that day. Refresh on foreground and at
  resident midnight (checked every 30 seconds).
- **History:** notes and ordinary/urgent notifications from /api/history, newest
  first. Load earlier activity follows the backend cursor. Shared across family.
- **Profile:** current family member, resident, timezone, connection, and sign-out.

The URLSession WebSocket connects to `/ws?app_user_id=...`. It refreshes household
reminders/history and the selected user's messages after relevant events; reconnect
re-fetches all sections. Bounded reconnect backoff and periodic refresh cover
socket outages. Account changes cancel the socket/tasks and invalidate late
responses, preventing the previous user's data from appearing in the new session.

POST retry keys are persisted locally by account/server/action and payload, and
reused after an uncertain timeout or app restart. Changing the payload starts a
new request. Success clears only that request's key. The composer retains failed
text; the reminder sheet dismisses only after a confirmed save.

Robot responses require the separate robot_backend v2 adapter. With dispatch
still disabled on app_backend, messages are durably queued and remain labeled
queued; the app does not invent responses.

## Files

| File | Responsibility |
|---|---|
| Models.swift | Typed v2 responses and time formatting |
| AnnieAPI.swift | URL query encoding, pagination, typed GET/POST, retry headers |
| AppConfiguration.swift | Build/env server URL |
| Profiles.swift, ProfileState.swift | Backend family selection and local profile |
| AppState.swift | Account-scoped loading, writes, errors, and event refresh |
| LiveConnection.swift | WebSocket, heartbeat, reconnect |
| Views.swift | Reminders, History, Profile |
| FamilyViews.swift | Today's Ask Annie conversation |

## Verify

```sh
swift test --package-path app_frontend
xcodebuild -project app_frontend/Annie.xcodeproj -scheme Annie \
  -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

Run these from the repository root using the Xcode toolchain. Tests mock network
responses and verify pagination, retry keys, failed-save behavior, account changes,
and timezone handling. A simulator build does not prove a physical phone connection.
