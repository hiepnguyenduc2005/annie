# Annie architecture

Two independently runnable FastAPI services separate application concerns from
robot-side processing. The app service runs on a server; the robot service runs
on the dog or a nearby computer, depending on its hardware.

## Architecture

```text
Users <-> Frontend <-> App service <-> Robot service <-> Robot hardware
                           |               |
                        Database      Speech / agent / video
                                           |
                                     Privacy gate -> Cloud models
```

- **App service:** user-facing API, users and robot ownership, approved signal and
  command-status persistence, and communication with connected robots.
- **Robot service:** speech-to-text, text-to-speech, local agent execution, video
  processing, robot hardware integration, and optional cloud escalation through
  a privacy gate.
- **Shared:** versioned message contracts. Each service owns its implementation;
  the robot service does not access the application database directly.

## Structure

```text
app_backend/             # Deploy to the app server
  app/
    main.py
    api/
    db/
    services/
    robot_gateway/
  requirements.txt
  README.md
robot_backend/           # Deploy to the dog or its local compute server
  app/
    main.py
    api/
    communication/
    speech_to_text/
    text_to_speech/
    agent/
    privacy/
    cloud/
    video/
    hardware/
  requirements.txt
  README.md
shared/                  # Proposed protocol definitions, not runtime coupling
frontend/                # User application
robot/                   # Firmware or hardware assets
```

Each backend owns its dependencies, Python environment, entrypoint, and deployment.
Neither backend imports the other or requires the other folder to start.
The shared signal model is a protocol reference, not yet integrated with either
service. Package or generate versioned validators before implementing transport.

Feature packages are placeholders. Only health endpoints and the shared message
model are implemented. Database storage, robot communication, authentication,
speech, agent execution, privacy enforcement, cloud escalation, video processing,
and hardware control are not wired up.

## Communication design

The proposed transport is a persistent WebSocket connection initiated by the
robot service to the app service, allowing commands and events in both directions.
This connection is not implemented yet.

1. A user submits a command through the frontend and app service.
2. The robot service processes input locally using speech, video, and its agent.
3. If local processing needs a cloud model, a privacy gate must approve a
   minimal nonpersonal request before the robot service sends it directly.
4. The robot sends only allowlisted status signals to the app service.
5. The app service validates and stores accepted signals in its database,
   deduplicating by signal ID, then updates the frontend.

### Privacy boundary

Raw audio, video, transcripts, identities, location, secrets, personal context,
and unrestricted model output stay on the robot. They must not be sent to cloud
models or the app backend, including through logs, errors, or telemetry.
Cloud escalation is optional: if a request cannot be shown to be safe and
nonpersonal, keep it local or report that it cannot be completed. Redaction alone
is not a guarantee; use narrowly defined approved request schemas and block
unknown content. The privacy gate must cover every outbound path.

The initial robot-to-app contract contains only a random signal ID, a fixed
status value, and a schema version. It has no free-text or arbitrary payload
field and rejects extra fields. Device authentication and routing belong to the
connection layer; do not add personal identifiers to signal bodies.
Even signal timing and connection metadata can reveal behavior, so retention,
access controls, and approved metadata need review before deployment.
This schema limits content; it is not a complete privacy enforcement system.

The app service's future database receiver must store only validated signals,
not raw rejected payloads. Database implementation and transport remain pending.
Robot microphone or camera input can also start local processing without an
app connection. Future transport work needs authentication, ownership checks,
acknowledgements, reconnects, duplicate handling, and explicit handling of expired
or offline commands. App-to-robot command schemas still need to be defined.
The app service must never act as a relay for private robot context.

## Run the backends

Follow [app backend setup](app_backend/README.md) and
[robot backend setup](robot_backend/README.md) in separate terminals or servers.
Each uses its own virtual environment and `requirements.txt`.

| Service | Local health | Local API docs |
| --- | --- | --- |
| App | http://127.0.0.1:8000/health | http://127.0.0.1:8000/docs |
| Robot | http://127.0.0.1:8001/health | http://127.0.0.1:8001/docs |

The entrypoint is `app.main:app` from within each backend folder. The previous
`backend/app_service` and `backend/robot_service` commands no longer apply.

## Next implementation decisions

Choose the robot SDK and onboard compute, database, speech providers/models,
local agent runtime, camera pipeline, and approved cloud request schemas before
implementing their adapters.
Then build one end-to-end command and safe status-signal persistence flow before adding
audio and video processing.
