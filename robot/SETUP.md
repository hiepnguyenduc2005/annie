# Physical Go2 Air bring-up

The first milestone is a stationary live camera frame, measured battery and
pose, and a clean disconnect. These checks do not establish physical navigation,
image inference, speech recognition, or audible playback.
The repository targets the Air edition; the photographed model plate says
Go2. Confirm the loaner's edition and firmware in the app before recording
hardware signoff.

## Wiring

```text
Go2 Air <--- local Wi-Fi / WebRTC ---> Mac or GX10
                                        |
                                        +--- USB ---> Anker PowerConf S330
```

The team's diagram assumed a USB audio host on the dog. The actual Air has no
accessible USB port according to the operator. The S330 is USB-only, so attach
it to the Mac/GX10 for bench testing. Sound and microphone capture then occur at
that computer. An untethered audio device carried on the dog remains separate
hardware work; do not claim the Air forwards S330 audio.

Keep the robot and its host on a local network that allows devices to reach
each other. A venue network may isolate clients. Start with the local robot
connection; a public domain, Internet relay, and external model are unnecessary
for this milestone. Full hardware images remain on the trusted local network.

## Existing Mac environment

From the repository root:

```sh
.cache/dimos/.venv/bin/python robot/host_check.py
.cache/dimos/.venv/bin/dimos go2tool discover --lan --timeout 8
.cache/dimos/.venv/bin/dimos go2tool discover --ble --timeout 8
```

`host_check.py` inventories packages, GPU tooling, and audio devices. It does
not record audio/video or contact a model. A listed device is not proof of
capture/playback. DimOS discovery is read-only, but its CLI can hide scanner
exceptions; an empty table alone does not establish healthy Bluetooth/networking.

If the robot is visible over Bluetooth but not on the host's network, use
`dimos go2tool connect-wifi` interactively and select the actual loaner. Enter
the network password at its hidden prompt, never in chat, Git, or command-line
arguments. Provisioning changes the selected robot's network. If Bluetooth finds nothing, verify the robot
in the Unitree app and obtain its network/IP there; do not guess an IP or
modify firmware to compensate for missing discovery.

## Fresh host

Use Python 3.12 and a separate environment. The stationary probe uses the
WebRTC driver directly; it does not need the full DimOS navigation stack.

```sh
uv venv robot/.venv --python 3.12
uv pip install --python robot/.venv/bin/python -r robot/requirements.txt
robot/.venv/bin/python robot/host_check.py
```

The pinned driver includes audio and OpenCV dependencies. On Ubuntu, PyAudio
may need `build-essential`, `portaudio19-dev`, and Python development headers;
OpenCV may need `libgl1` and `libglib2.0-0`. On macOS, PortAudio is required.
Report installation failures before changing an existing application runtime.
GX10 ARM64 installation and GPU/model compatibility remain to be verified on
the actual machine. These commands have not been executed on a GX10.

## Stationary connection check

Set `ROBOT_IP` to the discovered, confirmed loaner's private IPv4 address.
Then run the probe with the selected environment:

```sh
.cache/dimos/.venv/bin/python robot/go2_probe.py --ip "$ROBOT_IP" --timeout 20
```

The probe enables camera streaming and subscribes to telemetry. It sends no
stand, walk, velocity, mode-switch, or autonomous navigation command. It reports
only frame metadata and selected battery/pose fields, never frame bytes, raw
provider responses, or credentials. Receipt times are host times, not calibrated
robot capture timestamps or synchronized frame-to-pose evidence.

Firmware 1.1.15 and newer may require a per-device WebRTC AES key. Obtain it
through the owner/vendor's supported setup and provide `UNITREE_AES_128_KEY`
privately in the process environment. Do not put it in command arguments or
tracked files, and do not change firmware as a workaround. A missing key is
a connection blocker, not a reason to substitute simulated data.

## Audio and GX10 follow-through

1. Plug S330 into the selected computer and confirm both USB input and output
   in `host_check.py` (or `arecord -l` / `aplay -l` on Linux). Select those exact
   devices for a short operator-observed playback/capture test.
2. On GX10, inspect `nvidia-smi`, OS/architecture, and the actual installed
   image-capable model before selecting its runtime. A model name or health
   response alone does not prove image input works.
3. Use the existing [brain API](contract/brain.md) in local mode. Preserve
   hardware source, frame ID, capture timestamp and synchronized pose before
   connecting observations to policy. The probe is diagnostic, not that adapter.
4. Verify the physical stop/recovery procedure, then a supervised short motion
   test under the project's hardware acceptance gate (HW-01 through HW-05).
   Do not launch the full autonomous navigation blueprint to test connectivity.

## Sources and observed status

- [DimOS Go2 setup](https://github.com/dimensionalOS/dimos/blob/main/docs/platforms/quadruped/go2/setup.md):
  Air/Pro WebRTC support, Python 3.12, Ubuntu recommendation; macOS experimental.
- [Unitree WebRTC driver](https://github.com/legion1581/unitree_webrtc_connect):
  installed version 2.2.0, MIT license; direct connection and stream APIs.
- [Anker S330](https://uk.ankerwork.com/products/a3308): USB connection.

On 2026-09-19 this Mac had Python 3.12.13, DimOS 0.0.13.post1 and the WebRTC
driver 2.2.0. No USB audio device was enumerated. Initial bounded LAN and BLE
discovery found no robot, including a direct BLE scan that completed without
an exception. The operator confirmed the dog is present and powered on.
After startup, Bluetooth advertised the exact name on the loaner's label.
The earlier generic `Unitree` name was filtered out by DimOS's default name
prefixes. A GATT connection succeeded on the FFE0 service. A correctly chunked
legacy handshake received a plaintext `0xF1` response with BLE module version
3: this unit uses V3 authentication. The installed DimOS provisioning helper
expects a legacy encrypted reply and cannot complete this setup.

The operator initially encountered a region mismatch in the International app.
An owner-supplied per-device AES key subsequently authenticated V3 BLE and
returned the expected serial. A local CoreBluetooth wrapper around
[unitree_ui](https://github.com/legion1581/unitree_ui), revision
`3eb378b7adcf06a7773724efcbb4f58a8df98e11` (MIT), provisioned AP mode and received
the robot's ready acknowledgment. Credentials and machine-specific settings
remain in ignored, restricted local storage; they are not part of this guide.

Live WebRTC at the robot's AP address `192.168.12.1` passed the stationary
probe: a decoded 1280×720 camera frame, battery/IMU status, odometry pose, and
clean disconnect. The first complete run measured 46% battery. A subsequent
read-only query reported firmware **1.1.15**, motion-controller mode `mcf`,
obstacle avoidance enabled, and a decoded LiDAR voxel stream. The model plate
still identifies only Go2; the Air/Pro edition has not been independently read.
The Mac briefly joined the robot AP for each check, then returned to its prior
internet network. These observations establish transport and sensor access,
not sustained app integration or navigation.

A priority StopMove request was acknowledged while the robot was stationary
in 44.8 ms. That is request round-trip time, not measured stopping performance.
At that stage no movement command had been sent. The operator subsequently
reported a paired physical controller and requested supervised walking within
a five-metre boundary, then corrected that report: no physical controller is
available. The boundary, physical stop/recovery, and loss-of-link behavior
remain unverified.

The initially installed DimOS 0.0.13.post1 `stop_movement` implementation only
cancelled its host timer. A local backport emitted neutral joystick input;
offline checks verified explicit stop and timer expiry emit zero input, and
closed-loop handling returns safely. The isolated environment was subsequently
upgraded to DimOS 0.0.14 from upstream revision
`c1c3cdc9d2ee54ca72259465688395699d7d99a2`, which includes this correction.
The wheel used `DIMOS_ALLOW_MISSING_COCKPIT=1` because the checkout's optional
web build was absent. Adding `eclipse-zenoh==1.10.1` allowed the voxel mapper,
patrol module and full Go2 navigation blueprint to import on this Mac. Import
success is not a running or hardware-validated navigation stack.
The host-side stop cannot stop the robot over a lost link and is not a
validated robot-side watchdog.
Do not launch the full DimOS blueprint as a stationary test: its Go2 module
automatically sends stand/balance commands on startup. Hardware motion still
requires the stop and bounded-path checks in HW-04 through HW-06.

The existing local Qwen3-VL 2B brain also processed one actual hardware camera
frame in 3,819 ms through `/infer`. Camera and pose were paired by host receipt
time (26 ms apart); calibrated sensor synchronization remains unverified.
This was local image inference on the Mac, not a full planning turn, a GX10
test, or permission to treat its person classification as a collision sensor.
The robot was subsequently powered off by the operator; hardware attempts
stopped and the BLE backend disconnected.

The separate [patrol supervisor](patrol/README.md) supplies an immutable
boundary, sensor and planner freshness gates, latched faults, and one-command
receipt tracking. Its 44 pure supervisor tests and 11 mocked bridge cases pass;
these are software tests. The hardware adapter is not wired to it, and all
physical stop, loss-link, map-boundary and obstacle behavior attestations
remain false until controlled measurement. `missing_verifications` exposes
the unmet checks to the operator.

## Supervised commissioning follow-up

After the robot was powered back on, a stationary reconnect read 40% battery.
Short direct Sport Move and obstacle-controller requests were followed by
explicit priority StopMove and neutral inputs. A two-second low joystick test
measured 0.0968 m net odometry displacement and fresh near-zero velocity after
stopping. This does not independently establish gait: body settling and pose
estimation can change these values. A requested 0.20 m forward route timed out
after seven seconds with only 0.015 m forward progress; its return leg did not
run. No complete circle or autonomous patrol has been demonstrated.

Firmware `mcf` requires different telemetry decoding: `sportmodestate.mode`
can remain zero while `error_code` carries the active mode. The installed
upstream UI decodes raw value 100 as Free Walk and 1013 as balance stand.
Do not interpret mode zero as proof of idle on this firmware, or use the UI's
cached gait highlight as fresh execution evidence. Record raw state with XY
and heading traces for subsequent commissioning.

The last prepared FreeWalk API 2045 test read **23% battery** and aborted before
sending FreeWalk or joystick input. Its initial raw MCF state was already 100;
therefore selecting Free Walk is not an established fix. The earlier 25% local
script cutoff was a chosen test limit, not a manufacturer threshold. Unitree's
[current battery manual](https://marketing.unitree.com/article/en/Go2/Battery_Charger.html)
(Recommended use, page 8) recommends stopping below **40%** and replacing or
charging the battery. This is operating guidance, distinct from BMS hard
protection. Replace/charge before further motion tests; do not lower the local
cutoff to work around this guidance.

The tracked `go2_walk.py` commissioning tool now enforces a finite minimum
battery setting of 40–100%, defaulting to 40%, before opening a connection.

An active hardware task can also inhibit legacy CLI launchers by creating the
ignored `.cache/go2-private/motion-inhibit` file. While it exists, `go2_walk.py`
exits before opening a robot connection. This prevents queued CLI jobs from
starting during another task's diagnosis; it is neither a robot-side stop nor
a lock covering arbitrary SDK clients. Clear conflicting launch assignments
and establish current hardware readiness before removing the file.
Its 45 fake-only checks pass, but it is not qualified for physical patrol:
the current completion flag measures elapsed command duration rather than
route execution, and its stop observer does not yet require distinct fresh
post-stop samples. The MCF command path also needs hardware verification.
Do not treat this tool's completion flag as a successful walk or patrol.

The local Unitree UI uses **Connect → Access Point (Direct) → Drive / Joysticks**
after joining the robot's Wi-Fi. The left joystick translates; the right
joystick turns. Direct control requires no Unitree cloud login. This Mac's
single Wi-Fi connection cannot simultaneously remain on its previous internet
network and the robot AP. Changing to a shared network or providing a second
internet connection remains necessary for continuous connected assistant work.
The latest stationary checks detected the exact robot's Bluetooth advertisement,
but GATT connection timed out before Unitree authentication. Neither saved
hotspot name was found, and targeted LAN discovery returned no robot. NordVPN
was off; it is not the current diagnosed blocker. The Mac's internet connection
was restored, with no robot client remaining. The requested demo network is
MIT; the robot's address on that network has not been verified. Current battery
is unknown; 23% is only the last historical reading. A physical operator or
booth technician must restore the dog’s communications/hotspot before the next
connection attempt. The local
connection-help patch is preserved in [tooling](tooling/unitree-ui/connection-help.patch)
against the pinned Unitree UI revision above.

## Motion and perception tools (2026-09-19)

Two operator tools sit beside the probe. Both use the probe's connection path,
read `UNITREE_AES_128_KEY` from the environment only, and stop with sanitized
errors. Neither has run on the physical dog yet. Current stationary diagnostics
and operating constraints are recorded above; radio detection alone is not a
working robot connection.

- `robot/go2_walk.py`: an unqualified commissioning prototype for HW-04/HW-05.
  It schedules line or circle motion requests using speed and elapsed time;
  requested distance or laps are not verified physical execution. It attempts
  priority StopMove on exit, but its completion and stop-observation issues
  above remain unresolved. Its 45 fake-only tests do not qualify the MCF path
  or prove stopping on hardware. Legacy CLI launches are currently inhibited.
- `robot/go2_perception.py`: read-only perception loop. It streams the camera,
  runs the tracked keypoint posture detector on every new frame at 5 Hz, sends
  the latest frame to the local brain `/infer` once per second (one in flight,
  stale frames dropped, never re-dated) and publishes the cited perception and
  a measured `dog.status` to the app with `source: hardware`. It sends no motion
  command. 10 fakes-only tests. Capture times are host receipt times paired
  with odometry by receipt time, not calibrated synchronization.

- `robot/go2_host_voice.py`: the viewer's audio role for hardware. It polls the
  app for queued `say` commands, renders them with the offline `say` adapter,
  plays them on this host and reports `accepted/executing/completed/failed`
  receipts with `source: host`; when the app opens a reply window it runs the
  live VAD-gated microphone listener once per event. Motion commands are left
  in the queue and counted. Measured 2026-09-19 on this Mac against a throwaway
  app with `ANNIE_REQUIRE_AUDIO_RECEIPT=true`: ready in ~2 s, the check-in was
  spoken and completed inside the 8 s audio watchdog, the reply window opened,
  the microphone heard no speech in a quiet room, and the app recorded
  `checkin_no_reply` then `fall_confirmed`. A daemon started after the fall
  misses the watchdog while Whisper warms up, so start it first. 8 fakes-only
  tests.

## Verification and outstanding work

- [x] Host inventory executes in the existing Mac runtime.
- [x] Stationary probe: 21 offline tests pass, including malformed telemetry,
  early video delivery, timeouts, disconnect failure, and omission of vendor
  secrets. Tests use fake connections and never dial hardware.
- [x] Contract schema export check and frontend JavaScript syntax check pass.
- [x] Authenticate V3 BLE with the owner-provided per-device key.
- [x] Complete AP setup and verify the actual robot IP over WebRTC.
- [x] Run the stationary probe on the physical robot and read firmware 1.1.15.
- [ ] Verify an operator stop, moving-stop response, link-loss behavior, and
  the requested physical patrol boundary before enabling autonomous motion.
- [ ] Verify the GX10 runtime, local image inference and USB audio.
- [ ] Connect synchronized hardware evidence to the app and rehearse bounded motion.

The broader app test run during this milestone had 79 passes and 18 failures
in the concurrently edited delivery/recovery tests. This probe changes no app
policy; those failures remain separate integration work. No software test here
is evidence of a physical camera frame or audible output.
