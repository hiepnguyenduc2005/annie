# dimOS integration: what we use, what fits, what does not

Status as of 2026-09-20. dimOS **0.0.14**, installed at
`.cache/dimos/.venv/lib/python3.12/site-packages/dimos` (paths below are relative to that
directory unless they start with `robot/`). Everything in "Verified" was run offline in the
unit test. Nothing here was run on the robot, against the live stack, against a model, or
with a dimOS coordinator/MCP server started.

```bash
.cache/dimos/.venv/bin/python -m pytest robot/tests/test_dog_dimos.py -q     # 15 passed in ~5 s
```

## What we now use from dimOS

| Ours | dimOS code it calls | Verified offline |
| --- | --- | --- |
| `robot/dog/dimos/frontier.py` `next_frontier_goal(grid, pose_xy, yaw)` | `WavefrontFrontierExplorer.get_exploration_goal` and `msgs/nav_msgs/OccupancyGrid`, unmodified | two-room grid: goal (0.15, 0.0), just through the door into the unexplored room; closed room gives None; yaw picks between symmetric doors; explored-goal memory moves to the other door |
| `robot/dog/dimos/agent.py` `eldercare_skills()` | `skills/skills.py` `AbstractSkill`, `SkillLibrary` | 19 function tools (11 existing + 8 new); compositions and failure paths against an in-process fake link |
| `robot/dog/dimos/agent.py` `AnnieSkillModule` | `core/module.py` `Module`, `agents/annotation.py` `@skill`, `CAP_MOVEMENT` | `Module.get_skills()` fed to dimOS's own `_handle_tools_list`: 15 MCP tools with correct schemas and `dimos/uses: ["movement"]` |
| `robot/dog/dimos/agent.py` `annie_agent_blueprint()` | `autoconnect`, `McpServer.blueprint()`, `McpClient.blueprint()` | the blueprint object builds. It was NOT started |

## Findings

### 1. Frontier exploration runs on our grid, standalone (a, b)

Inputs (`navigation/frontier_exploration/wavefront_frontier_goal_selector.py`): streams
`global_costmap: In[OccupancyGrid]`, `odom: In[PoseStamped]`, `goal_reached`, `explore_cmd`,
`stop_explore_cmd`, `stop_movement` (:114-119). No `Robot` object and no ROS. Output:
`goal_request: Out[PoseStamped]` (:122), a goal **position** (orientation fixed to identity,
:803-810). It never outputs velocities; dimOS hands the goal to its own planner
(`navigation/replanning_a_star`) and waits for `goal_reached` or `goal_timeout` (:823).

The core is callable without any of that: `get_exploration_goal(robot_pose: Vector3, costmap)`
(:622) takes a position and an `OccupancyGrid` built from a numpy int8 array
(`msgs/nav_msgs/OccupancyGrid.py:151`; -1 unknown, 0 free, 100 occupied, :123-135; indexed
`[y, x]`; origin = world position of cell (0,0), :280-297). Our grid converts with a transpose
and a half-window origin shift (`to_dimos_costmap`; a test checks cell-for-cell agreement).

Two things had to be ours:

- **Transport.** A dimOS `Module` opens its RPC transport in the constructor
  (`core/module.py:157-164`); a bare `WavefrontFrontierExplorer()` opened a Zenoh peer session
  on a loopback port (observed). `rpc_transport` is a config field (`core/module.py:107`), so
  `frontier._OfflineRPC` (a no-op `RPCSpec`) makes it a plain object: construction 5 ms, no
  sockets, no threads of its own.
- **Yaw.** dimOS takes no heading. Its only directional term is `exploration_direction`
  momentum (:443-467), 5 % of the score (:568-574). We seed it from yaw when unset.

Measured on the 120x120 (12 m, 0.1 m) grid on this Mac: **0.40 s per goal** (median of 5). It
is a pure-Python BFS over free AND unknown cells (:405-412), so call it at goal rate, not per
control tick.

**What does not fit, and it matters:**

- Our grid's free space is only the dog's walked footprint (`robot/go2_smart_patrol.py`
  `OccupancyGrid.visit`, 0.25 m radius), not LiDAR-observed empty space. On a thin walked
  trail dimOS returns the centroid of the trail's outline, a point ON the path already walked
  (measured: trail x -2..2 gives goal (0.0, 0.05)). `clear_range_m` adds ray clearing from the
  current pose, but that is our approximation: it treats "no obstacle evidence" as observed
  free.
- The goal is the **centroid** of a connected frontier (:302-311, :393-399). In open unknown
  space the frontier is a closed ring around the cleared area and its centroid is the robot
  (measured: (1.66, 0.0) for a dog at (2.0, 0.0)). It behaves well only where walls cut
  frontiers into arcs (frontier cells touching an occupied cell are rejected, :264-266).
- BFS crosses unknown cells, so a frontier is never checked for reachability through free
  space; dimOS relies on its planner to fail and time out.
- dimOS defaults (`safe_distance` 3 m, `lookahead_distance` 5 m, :88-89) are building-scale;
  `HOME_SCALE_CONFIG` scales them down. Those values are a guess, not tuned on the robot.

To use this live we still need a goal-to-(vx, wz) follower under the existing guardrails, and
a costmap with real free space. dimOS has one: `mapping/pointclouds/occupancy.py`
(`simple_occupancy`, `general_occupancy`, `height_cost_occupancy`, :143-427) and the
`CostMapper` module (`mapping/costmapper.py:44-48`), but they take a dimOS `PointCloud2`, not
our voxel arrays. Not attempted.

### 2. The installed dimOS agent does not take a SkillLibrary (c)

- `agents/ollama_agent.py` is 39 lines of helpers (`ensure_ollama_model`, `ollama_installed`);
  there is no Ollama agent class. `agents/vlm_agent_spec.py` is a Protocol with one method,
  `query_image` (:21-24); `VLMAgent` (`agents/vlm_agent.py:38`) answers image questions and has
  no tools.
- The tool-using agent is `McpClient` (`agents/mcp/mcp_client.py:90`), a LangGraph
  `create_agent` (:260-277). Its tools come from **one** MCP server,
  `McpClientConfig.mcp_server_url` (:86), via JSON-RPC `initialize` + `tools/list` (:174-205).
  The model is any LangChain `init_chat_model` id (:55-79); `langchain_ollama` is installed, so
  `ollama:<model>` is accepted. Not run here.
- That MCP server is dimOS's `McpServer` (`agents/mcp/mcp_server.py:347`). It lists the
  `@skill` methods of every module in the blueprint (`on_system_modules`, :381-394, through
  `Module.get_skills()`, `core/module.py:462-486`). The canonical wiring is
  `autoconnect(McpServer.blueprint(), McpClient.blueprint())` (`agents/demo_agent.py:23`).
- `AbstractSkill`/`SkillLibrary` is the legacy surface: inside dimOS only `skills/speak.py`,
  `skills/kill_skill.py`, `skills/rest/rest.py` and `robot/unitree/b1/unitree_b1.py` import it.
  So `robot/dog/missions/skills.py` is valid dimOS code that no dimOS agent can consume.

Hence two surfaces in `agent.py`: the requested `AbstractSkill` compositions, and
`AnnieSkillModule`, whose `@skill` methods delegate to the same library and the same body
contract. For the sponsor track the second one is the one that counts.

**MCP attachment (Elastic idea), TODO not built.** There is no list of servers; one URL is
wired through `_mcp_request` (:129-146) and `_mcp_tool_to_langchain` (:207-234). Options:
(1) subclass `McpClient`, add `extra_mcp_server_urls` plus per-server auth headers read from
the environment, override `_fetch_tools` to merge each server's `tools/list` and bind each
`StructuredTool` to its own URL; (2) no dimOS change: a `@skill` on `AnnieSkillModule` that
queries Elasticsearch through `robot/dog/memory/elastic.py` and returns text. (2) is smaller
and keeps the API key out of the agent. The Elastic MCP endpoint and auth format were not
checked.

### 3. dimOS bug that reaches our existing skills: shared `SkillLibrary._instances`

`skills/skills.py:107` declares `_instances` as a **class** attribute and `create_instance`
never overwrites a key (:113). The first library built in a process pins its constructor
arguments for every later one. Reproduced with two in-process fake links and the existing
`annie_skills()`: `Stop` called on the second library was delivered to the FIRST link. So a
simulator or test library built after a live one drives the live body, including its stop.
`eldercare_skills()` guards with `lib._instances = {}` and has a regression test.
`robot/dog/missions/skills.py::annie_skills` has the same exposure and was outside this
change's write scope; the fix is the same one line after `SkillLibrary()`.

### 4. Observation memory fits our sightings (d)

`memory/type/observation.py:106-122`: `Observation(id, ts, data_type, pose_tuple, tags, data)`;
`pose_tuple` is `(x, y, z, qx, qy, qz, qw)` and a 3-tuple is padded with the identity
quaternion (:63-81). A sighting maps directly: `ts` = time, `pose` = (x, y, z),
`tags` = {label, identity, posture}, `data` = the label or a small dict. Queries match ours:
`TimeRangeFilter`, `NearFilter(position, radius)`, `TagsFilter` (`memory/type/filter.py:61-100`),
ordering, limit, substring search, and vector search over `EmbeddedObservation`
(`memory/embed.py` `EmbedImages`/`EmbedText`, which need a dimOS `EmbeddingModel`). Backends:
`ListObservationStore` (in memory, `memory/observationstore/memory.py:37`) and
`SqliteObservationStore` (`sqlite.py:222`). This was read, not run: no adapter was written.
It would replace the recorder's people log and its JSONL, not the pose track, the obstacle
frames, the 4D viewer or the Elasticsearch index. `memory/replay.py` replays recorded dimOS
stores, not our JSONL.

## What dimOS provides that we hand-rolled

| Capability | dimOS | Replace ours? | Effort / risk |
| --- | --- | --- | --- |
| Frontier exploration | `WavefrontFrontierExplorer` | Goal selection only, and only with a ray-cleared costmap | Adapter done. Live use needs a goal follower and real free space: about a day; risk medium (centroid goals, 0.4 s/call) |
| Occupancy mapping | `mapping/pointclouds/occupancy.py`, `CostMapper`, `mapping/occupancy/inflation.py` | Would give true free space | Needs voxel map to `PointCloud2`; 0.5-1 day; not attempted |
| Patrolling | `navigation/patrolling/module.py:38` (coverage, frontier, random routers) | No | Same costmap + `goal_request` + planner dependency (:39-42); our reactive patrol needs neither |
| Observation memory | `memory/observationstore` | Partly (sightings) | Schema fits; 2-3 h for an adapter; low risk |
| Agent + MCP | `McpClient` + `McpServer` | Additive: a second brain, not a replacement for guardrails | Blueprint built, never started. First start binds MCP port 9990 and needs a model: 1-2 h to first tool call if the model is up; medium risk on demo day |
| FollowHuman | `skills/visual_navigation_skills.py:38` | No | Needs `robot.person_tracking_stream` and `robot.move(Vector)` (:72, :86, :111), a legacy Robot API our body does not have. The current version, `agents/skills/person_follow.py:52`, needs `color_image`, `global_map`, `tf` streams (:64-67) |
| Visual servoing | `navigation/visual_servoing/visual_servoing_2d.py:22` | Possible | Pure: `compute_twist(bbox, image_width)` (:70) returns a `Twist`; needs a dimOS `CameraInfo`. Not tried; our follow controller already works live |
| Speak | `skills/speak.py:77` | No | Requires a dimOS `tts_node` (:82-90). Ours goes through the body `say` command to the robot speaker |

## Not examined

`mapping/ray_tracing`, the patrol routers' internals, `robot/unitree/go2` blueprints and
connection (dimOS's own Go2 WebRTC stack, which would compete with our link for the single
WebRTC session), `agents/mcp/tool_stream.py`, `memory/store` (mcap), `perception/`.

## Caveats for whoever runs this next

- `robot/dog/dimos` is a package named `dimos`. Imports resolve to the installed dimOS only
  because `robot/dog` is never on `sys.path`. Do not run Python with `robot/dog` as the working
  directory or as a path entry.
- `DEFAULT_BODY_URL` is `http://127.0.0.1:8011`, the live dog. `AnnieSkillModule()` and
  `eldercare_skills()` without an explicit link talk to it. The tests always pass a fake.
- The elder-care skills return transcripts, acknowledgments and memory estimates. None of
  them confirms a motion, a person's condition, or that medication was taken, and their
  strings say so.
