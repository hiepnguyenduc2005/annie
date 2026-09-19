# Live home and resident specification

All implementation, UI, API adapters and tests stay under `robot/`.

The new `grandmas-house` scene is a 14 × 12 m furnished, two-storey home.
Ground-floor zones include living room, study, bedroom, kitchen/dining,
bathroom/utility, garden room, stair hall and entrance hall. The upper floor
has its own slab, rooms, furniture and an open stairwell. Eighteen physical
steps rise 3 m. The overview can hide the upper floor; the robot camera
always renders the actual scene, including occlusion by that floor.

## Resident life

`daily_life.py` animates thirteen mocap body parts: grandma reads, walks to
breakfast, eats, walks to bed, rests, and returns to the living room.
The cycle lasts about 130 s. Walking is 0.4 m/s; dwell times are shortened
for the demonstration. Limbs swing while walking; the avatar has grey hair
and glasses. These are authored actor motions, not learned human behavior.
They never modify robot joint positions, controls, or vision classifications.

Pause freezes simulation time. Reset restarts the routine. **Stage a fall**
lowers the actor at her current position; **Recover** stands her up and
resumes the interrupted routine. This animation is a scenario stimulus,
not a physically validated human fall or an injury simulation.

## Robot execution

A trained DimOS Go1 policy actuates twelve joints in MuJoCo. An authored
collision map routes to seven ground-floor waypoints. All seven have
collision-free paths in the generated scene. A local camera person detector
can inhibit motion. A separately labeled simulator proximity guard also
stops the dog near the moving avatar; it is an oracle collision precaution,
not calibrated visual ranging.

The vision-language planner chooses goals and actions; the A* planner only
executes an admissible chosen destination. Evidence and measured command
outcomes return to the model. See [AI brain](AI_BRAIN.md).

Normal-height stair traversal is **not supported** by the current policy.
The generated stairs have collisions, but no upstairs command is exposed.
[Measured stair failures](STAIRS.md) describe the required controller work.
There is no robot teleport or hidden lift between floors.

## Generate and run

From the repository root, after the existing model/asset setup:

```sh
.cache/dimos/.venv/bin/python -m robot.simulation.house
.cache/dimos/.venv/bin/python robot/simulation/viewer.py \
  --model .cache/menagerie/unitree_go2/scene.xml \
  --scenes .data/simulation/scenes/manifest.json \
  --locomotion --person-safety --native-audio --port 8766
```

The factory button preserves the six fixture categories and adds this house
to each new batch. The house appears first. Source code is procedural; cached
furniture and textures retain the existing asset manifest provenance.

Acceptance checks: model stays 19 qpos / 18 qvel / 12 actuators; actor
animation changes only mocap transforms; replay at the same time is
repeatable; each ground-floor waypoint is reachable; stair treads and the
landing are contiguous. Run `test_house_life.py` in the simulator environment.
