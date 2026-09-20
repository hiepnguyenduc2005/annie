"""robot/dog/dimos: dimOS frontier exploration on Annie's grid, elder-care skills, native @skill module.

Offline: no body service, no model, no MCP server, no sockets opened by dimOS (the frontier
explorer runs with a no-op RPC transport). The skill tests use an in-process fake BodyLink.
Run from the dimOS venv:
  .cache/dimos/.venv/bin/python -m pytest robot/tests/test_dog_dimos.py -q
"""
import json
import math
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest.importorskip("dimos")
from robot.go2_smart_patrol import OccupancyGrid  # noqa: E402
from robot.dog.dimos import frontier  # noqa: E402

CELL = 0.1


def _fill(arr, grid, x0, x1, y0, y1, value):
    """Set cells covering the world rectangle [x0, x1) x [y0, y1) (our grid is indexed [x, y])."""
    i0, j0 = grid._cell(x0 + 1e-6, y0 + 1e-6)
    i1, j1 = grid._cell(x1 - 1e-6, y1 - 1e-6)
    arr[i0:i1 + 1, j0:j1 + 1] = value


def two_rooms(doors=("east",)):
    """Room A (x -5..0, y -2.5..2.5) walked everywhere; room B beyond the east wall never seen.

    Walls are obstacle evidence, the interior is `visited`, each door is a 1 m visited gap.
    """
    grid = OccupancyGrid((0.0, 0.0), size_m=12.0, cell_m=CELL)
    wall = grid.threshold + 3.0
    _fill(grid.obstacle, grid, -5.1, 0.1, -2.6, 2.6, wall)       # solid block ...
    _fill(grid.obstacle, grid, -5.0, 0.0, -2.5, 2.5, 0.0)        # ... hollowed: a one-cell wall ring
    _fill(grid.visited, grid, -5.0, 0.0, -2.5, 2.5, 1)
    if "east" in doors:
        _fill(grid.obstacle, grid, 0.0, 0.1, -0.5, 0.5, 0.0)
        _fill(grid.visited, grid, 0.0, 0.1, -0.5, 0.5, 1)
    if "west" in doors:
        _fill(grid.obstacle, grid, -5.1, -5.0, -0.5, 0.5, 0.0)
        _fill(grid.visited, grid, -5.1, -5.0, -0.5, 0.5, 1)
    return grid


def test_costmap_conversion_matches_dimos_conventions():
    grid = two_rooms()
    costmap = frontier.to_dimos_costmap(grid)
    assert costmap.grid.shape == (120, 120) and costmap.resolution == pytest.approx(CELL)
    assert (costmap.origin.position.x, costmap.origin.position.y) == (-6.0, -6.0)
    for (x, y), expected in {(-2.55, 0.05): 0, (-5.05, 1.05): 100, (3.05, 0.05): -1, (0.05, 0.05): 0}.items():
        gx, gy = (int(v) for v in (costmap.world_to_grid((x, y, 0.0)).x, costmap.world_to_grid((x, y, 0.0)).y))
        assert costmap.grid[gy, gx] == expected, (x, y)          # dimOS indexes [y, x]
        assert (gx, gy) == grid._cell(x, y)                      # same cell as our own indexing


def test_goal_lands_in_the_unexplored_rooms_frontier():
    grid = two_rooms()
    goal = frontier.next_frontier_goal(grid, (-2.5, 0.0), 0.0)
    assert goal is not None
    x, y = goal
    assert 0.0 <= x <= 0.4 and abs(y) <= 0.5, goal               # just through the east door, on its axis
    i, j = grid._cell(x, y)
    assert grid.visited[i, j] == 0 and grid.obstacle[i, j] < grid.threshold   # an unknown cell, room B side


def test_closed_room_has_no_frontier():
    assert frontier.next_frontier_goal(two_rooms(doors=()), (-2.5, 0.0), 0.0) is None


def test_yaw_breaks_the_tie_between_symmetric_doors():
    grid = two_rooms(doors=("east", "west"))
    east = frontier.next_frontier_goal(grid, (-2.5, 0.0), 0.0)
    west = frontier.next_frontier_goal(grid, (-2.5, 0.0), math.pi)
    assert east[0] > -0.2 and west[0] < -4.8, (east, west)


def test_planner_remembers_explored_goals_and_prefers_the_other_door():
    grid = two_rooms(doors=("east", "west"))
    planner = frontier.FrontierPlanner()
    first = planner.next_goal(grid, (-2.5, 0.0), 0.0)
    second = planner.next_goal(grid, (-2.5, 0.0), 0.0)            # unchanged map: dimOS penalises the explored goal
    assert first[0] > -0.2 and second[0] < -4.8, (first, second)


def test_ray_clearing_pushes_the_goal_into_the_unexplored_room():
    grid = two_rooms()
    plain = frontier.next_frontier_goal(grid, (-1.0, 0.0), 0.0)
    cleared = frontier.next_frontier_goal(grid, (-1.0, 0.0), 0.0, clear_range_m=3.0)
    assert cleared is not None and cleared[0] > plain[0] + 0.5, (plain, cleared)   # frontier moved into room B


def test_offline_explorer_opens_no_transport():
    planner = frontier.FrontierPlanner()
    assert type(planner.explorer.rpc).__name__ == "_OfflineRPC"
    assert not any("zenoh" in t.name.lower() or "lcm" in t.name.lower() for t in threading.enumerate())


# ---- elder-care skills and the dimOS-native module (in-process fake link: no sockets, never the live dog) ----
from robot.dog.dimos import agent  # noqa: E402


class FakeLink:
    """Stands in for BodyLink. Records commands; `fail` names commands that come back failed."""
    base_url, headers = "http://fake.invalid", {}

    def __init__(self, fail=(), transcript="I am fine, thank you"):
        self.log, self.fail, self.transcript = [], set(fail), transcript

    def run(self, name, args=None, deadline_s=0.0):
        self.log.append((name, dict(args or {})))
        if name in self.fail:
            return {"name": name, "state": "failed", "error": "not found"}
        result = {"find_person": {"found": True}, "say": {"played": True},
                  "listen": {"heard": bool(self.transcript), "transcript": self.transcript}}.get(name, {})
        return {"name": name, "state": "completed", "result": result}

    def stop(self):
        self.log.append(("stop", {}))
        return {"stop_code": 0}

    def go_home(self):
        self.log.append(("go_home", {}))
        return "going home"


def memory(people=(), events=(), age_s=5.0):
    import time
    return agent.MemoryView(fetch=lambda: {"t": time.time(), "pose": [0, 0, 0], "people_t": time.time() - age_s,
                                           "people": list(people), "events": list(events), "frame": None})


def test_library_exposes_nineteen_function_tools():
    tools = agent.eldercare_skills(FakeLink(), memory()).get_tools()
    names = [t["function"]["name"] for t in tools]
    assert len(names) == 19 and len(set(names)) == 19
    assert {"CheckOnPerson", "DeliverMessage", "MedicationReminder", "WhereIs", "FindObject", "EscortToHome",
            "NightCheck", "WaveAtEveryone", "FindPerson", "Say", "Stop"} <= set(names)
    tool = next(t for t in tools if t["function"]["name"] == "DeliverMessage")
    assert tool["type"] == "function" and tool["function"]["strict"] is True          # OpenAI function-calling shape
    params = tool["function"]["parameters"]
    assert set(params["properties"]) == {"person", "text", "timeout_s"} and params["type"] == "object"
    assert "family message" in tool["function"]["description"]
    print("\nget_tools()[DeliverMessage] =", json.dumps(tool, indent=1))


def test_check_on_person_is_find_then_ask_then_listen():
    link = FakeLink()
    out = agent.eldercare_skills(link, memory()).call("CheckOnPerson", person="Jeanine")
    assert [name for name, _ in link.log] == ["find_person", "say", "listen"]
    assert link.log[0][1]["name"] == "Jeanine" and "are you alright" in link.log[1][1]["text"]
    assert "I am fine, thank you" in out


def test_failures_are_reported_and_nothing_is_said_to_nobody():
    link = FakeLink(fail={"find_person"})
    lib = agent.eldercare_skills(link, memory())
    out = lib.call("DeliverMessage", person="Jeanine", text="Dinner is at six")
    assert "not delivered" in out and [n for n, _ in link.log] == ["find_person"]      # never spoke
    silent = agent.eldercare_skills(FakeLink(transcript=""), memory())
    assert "no reply heard" in silent.call("CheckOnPerson", person="Jeanine")
    reminder = agent.eldercare_skills(FakeLink(), memory()).call("MedicationReminder", person="Jeanine")
    assert "not confirmation that medication was taken" in reminder


def test_memory_skills_answer_from_the_view_and_degrade_without_it():
    seen = memory(people=[{"identity": "Jeanine", "label": "person", "x": 1.23, "y": -0.5, "posture": "sitting"}],
                  events=[{"kind": "object", "text": "glasses on the side table", "x": 2.0, "y": 1.0}])
    lib = agent.eldercare_skills(FakeLink(), seen)
    where = lib.call("WhereIs", person="jeanine")
    assert "x=1.2 m" in where and "y=-0.5 m" in where and "sitting" in where and "s ago" in where
    assert "no sighting of Bob" in lib.call("WhereIs", person="Bob")
    assert "side table" in lib.call("FindObject", label="glasses")
    assert "not implemented" in lib.call("FindObject", label="keys")
    offline = agent.eldercare_skills(FakeLink(), agent.MemoryView(base_url=""))
    assert "memory unavailable" in offline.call("WhereIs", person="Jeanine")


def test_escort_night_check_and_waves_compose_body_commands():
    link = FakeLink()
    lib = agent.eldercare_skills(link, memory(people=[{"identity": "Jeanine", "posture": "lying"}]))
    assert "going home" in lib.call("EscortToHome", person="Jeanine") and ("go_home", {}) in link.log
    night = lib.call("NightCheck", duration_s=30.0)
    assert "saw Jeanine (lying)" in night and ("patrol", {"duration_s": 30.0}) in link.log
    assert lib.call("WaveAtEveryone", rounds=2) == "waved 2 time(s)"
    assert [n for n, _ in link.log].count("hello") == 2


def test_native_module_skills_reach_dimos_mcp_tools_list():
    """The surface the installed dimOS agent actually reads: Module.get_skills() -> McpServer tools/list."""
    from dimos.agents.mcp.mcp_server import _handle_tools_list

    link = FakeLink()
    module = agent.AnnieSkillModule(link=link, memory=memory(), rpc_transport=frontier._OfflineRPC)
    skills = module.get_skills()
    listed = _handle_tools_list(1, skills)["result"]["tools"]      # dimOS's own handler, unmodified
    by_name = {t["name"]: t for t in listed}
    assert len(by_name) == 15 and {"check_on_person", "deliver_message", "where_is", "stop_moving"} <= set(by_name)
    assert set(by_name["deliver_message"]["inputSchema"]["required"]) == {"person", "text"}
    assert by_name["find_person"]["_meta"]["dimos/uses"] == ["movement"] and "_meta" not in by_name["say"]
    assert "I am fine" in module.check_on_person(person="Jeanine")
    assert [n for n, _ in link.log] == ["find_person", "say", "listen"]
    print("\ntools/list[deliver_message] =", json.dumps(by_name["deliver_message"], indent=1))


def test_agent_blueprint_builds_without_starting_anything():
    blueprint = agent.annie_agent_blueprint(model="ollama:qwen3:8b", body_url="http://fake.invalid", view_url="")
    text = repr(blueprint) + str(getattr(blueprint, "__dict__", ""))
    for name in ("AnnieSkillModule", "McpServer", "McpClient"):
        assert name in text, name


def test_two_libraries_in_one_process_keep_their_own_links():
    """Guards dimOS's class-level SkillLibrary._instances (skills/skills.py:107): the first link must not leak."""
    first, second = FakeLink(), FakeLink()
    lib_a, lib_b = agent.eldercare_skills(first, memory()), agent.eldercare_skills(second, memory())
    lib_b.call("Say", text="hello")
    assert first.log == [] and second.log == [("say", {"text": "hello"})]
    lib_a.call("Stop")
    assert first.log == [("stop", {})]
