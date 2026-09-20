# Annie over MCP

`robot/dog/mcp_server.py` is a Model Context Protocol server named `annie`. Any MCP client (Claude Code, Claude
Desktop, an agent framework with a stdio MCP client) can see what the dog is doing, ask what she remembers, and
tell her what to do.

> **This drives a real robot.** Every command tool moves or speaks through the Go2 in someone's home. Keep the
> dog in sight, keep the remote within reach, and read [Safety](#safety) before the first call.

## How it fits

```
MCP client  --stdio-->  robot/dog/mcp_server.py  --HTTP-->  dog process (default :8111 simulator)
```

The server is a thin, stateless bridge: each tool is one or two HTTP calls to the running dog process, the same
API the command-center page uses. The dog process still owns the robot, validates every command against the body
contract (`robot/dog/runtime/body.py: validate_command`) and keeps the guardrails (collision, stall, battery,
leash, link). The MCP server cannot do anything the page cannot. It imports only `httpx` and `mcp`, so it starts
without dimOS, torch or the WebRTC driver.

The dog process must already be running. Start the isolated simulator with
`.venv/bin/python robot/demo_sim.py` ([runbook](SIM_DOG.md)). The default is port **8111**;
physical sessions on **8011** require an explicit `ANNIE_BODY_URL`. With the process off, every tool answers
`{"ok": false, "error": "the dog process is not reachable"}`.

## Add it to a client

Claude Code, from the repo root (a project-scoped entry is already checked in as `.mcp.json`, so opening the repo
in Claude Code offers the server; approve it once):

```bash
claude mcp add annie -- .venv/bin/python robot/dog/mcp_server.py
# a dog on another host, or one that checks a token:
claude mcp add annie -e ANNIE_BODY_URL=http://127.0.0.1:8011 -e ANNIE_BODY_TOKEN=... -- .venv/bin/python robot/dog/mcp_server.py
```

Claude Desktop (`claude_desktop_config.json`); use absolute paths, Desktop does not start in the repo:

```json
{"mcpServers": {"annie": {"command": "/path/to/Annie/.venv/bin/python",
                          "args": ["/path/to/Annie/robot/dog/mcp_server.py"],
                          "env": {"ANNIE_BODY_URL": "http://127.0.0.1:8111"}}}}
```

Install once: `uv pip install --python .venv/bin/python -r robot/requirements.txt` (adds `mcp==2.2.0`; the server
runs on the 1.x `FastMCP` and the 2.x `MCPServer` API).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANNIE_BODY_URL` | `http://127.0.0.1:8111` | Simulator by default; explicitly set the physical process URL when intended |
| `ANNIE_BODY_TOKEN` | unset | Sent as `X-Body-Token`; the dog process checks it on every POST when it has one. Keep it in the client's env, never in a tracked file |

Every HTTP call has a 5 s timeout and ignores proxy settings.

## Tools

Read-only:

| Tool | What it does | Dog API |
| --- | --- | --- |
| `dog_status()` | Live state, recent greetings, missions, memory sentences, voice setup, concerns | `GET /telemetry.json` (trimmed) |
| `memory(limit=6)` | What Annie remembers as sentences, plus recent events | `GET /telemetry.json`, `GET /graph.json` |
| `where_is(name)` | Last sighting of a person or object: position (odom frame, metres), place, age, posture | `GET /graph.json` entities |
| `people()` | The people Annie knows by name | `GET /people` |
| `command_status(command_id)` | The receipt of an earlier command | `GET /command/{id}` |
| `voice_settings()` | With no arguments: the current voice setup | `GET /voice` |

Commands (the robot moves or speaks):

| Tool | What it does | Dog API |
| --- | --- | --- |
| `stop()` | Cancel the mission and hold still. **Software stop**, see below | `POST /stop`, then `POST /command {"action": "stop"}` |
| `instruct(text)` | Plain-language instruction; the dog plans it into validated steps, elder-care skills included (`robot/dog/planning/care_skills.py`). Returns the receipt at once | `POST /command {"name": "instruct", "args": {"text", "author": "mcp"}}` |
| `command(action)` | `explore`, `scan`, `go_home`, `stop`, `hello`, `dance`, `sit`, `stand`, `follow` | `POST /command {"action"}` |
| `say(text)` | Speak up to 300 characters; waits up to 30 s | `POST /command {"name": "say"}` + poll |
| `listen(max_s=8)` | Listen 1-15 s; transcript in `result.transcript` | `POST /command {"name": "listen"}` + poll |
| `find_person(name?, approach=true)` | Search for a person and walk up to them; waits up to 120 s | `POST /command {"name": "find_person"}` + poll |
| `look_for(thing)` | Scan with the camera for a thing or place and walk toward it; waits up to 120 s | `instruct` with the text `go to the <thing>` |
| `remember_person(name, relation?, shirt?)` | Add or update a known person. No photos over MCP; faces are enrolled in the family app | `POST /people` |
| `voice_settings(cloud?, input_device?, output_device?)` | Cloud voices on/off, microphone and speaker by name. API keys are never sent or returned | `POST /voice` |

Resources: `annie://status` (the `dog_status` result) and `annie://people`, both JSON.

### Results

Every tool returns a JSON object with `ok`. Command tools return the dog's receipt:
`{"ok", "command_id", "name", "state", "result", "error"}` with `state` one of `accepted`, `queued`, `executing`,
`completed`, `failed`, `cancelled`. If a wait runs out the state is still `executing` and the `note` says to poll
`command_status` (or call `stop`). Failures are readable rather than exceptions:

If the submission response is lost, the result includes its generated `command_id` and
`state: "unknown"`. Poll that ID before deciding what to do; do not resend the same action.

| Situation | `error` |
| --- | --- |
| Dog process off, wrong URL, timeout | `the dog process is not reachable` |
| Wrong or missing token (401) | `the dog process rejected the token: set ANNIE_BODY_TOKEN ...` |
| Mission queue full (409) | `the dog is busy and its queue is full: wait, or call stop` |
| Part of the dog process not running (503) | `that part of the dog process is off (graph off)` |
| The dog refused the input (400) | `the dog process refused it: <its reason>` |

Overlapping commands queue in the dog process (up to 8) instead of failing, so a second `say` during a
`find_person` comes back `queued`.

## Safety

- **`stop` is a software stop.** It cancels the running mission, the patrol loop sends StopMove, and mission
  execution stays paused until a new mission is submitted. It is **not** a hardware emergency stop and it depends on
  the Mac, the dog process and the Wi-Fi link all working. If the dog must stay stopped, use the remote or lift it.
  If `stop` answers `STOP WAS NOT DELIVERED`, do that immediately.
- Tools are async, so a client can call `stop` while `find_person` is still waiting.
- A receipt is an acknowledgment, a transcript or a perception estimate. `completed` is not proof that a motion
  happened, `where_is` is a remembered estimate with an age (not a live fix), and nothing here is a health
  assessment: a reminder never confirms that medication was taken.
- The guardrails stay in the dog process and win over any MCP client. An MCP client cannot change Wi-Fi, keys or
  guardrail settings.
- `say` speaks out loud in a resident's home, and `remember_person` stores a name. Treat both as acting on a
  person, not on a test fixture.

## Limits

- stdio only. dimOS's own agent (`McpClient`) reads tools from a single HTTP MCP URL and already gets Annie's
  skills from `AnnieSkillModule` (`docs/DIMOS_INTEGRATION.md`); pointing it at this server needs an HTTP transport
  (`mcp.run(transport="streamable-http")`), which is not wired up or tested.
- `look_for` has no body command of its own, so it rides on `instruct`. Without a model the keyword rules turn
  `go to the <thing>` into one `look_for` step (covered by a test); with a model the planner chooses the steps.
- `memory` is bounded by the dog process: at most 6 sentences.
- Not run against the physical dog in this change. Verified by `robot/tests/test_dog_mcp_server.py`: request
  shapes against a fake dog process (`httpx.MockTransport`), the error paths, and a stdio smoke test that starts
  the server with the real MCP client and lists its tools.

```bash
.venv/bin/python -m pytest robot/tests/test_dog_mcp_server.py -q
```
