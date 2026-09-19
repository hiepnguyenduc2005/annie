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
3. Use the existing [brain API](../contract/brain.md) in local mode. Preserve
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
After the operator completed startup, Bluetooth advertised the exact name on
the loaner's label. The earlier generic `Unitree` name was filtered out by
DimOS's default name prefixes. The legacy BLE setup handshake then timed out;
no network settings were changed. Use the official [Unitree Go app](https://www.unitree.com/app/go2/)
for initial Wi-Fi pairing. The Mac's current network did not return the robot
to a directed multicast discovery probe. Physical WebRTC connection, GX10
access, audio and motion remain unverified.

## Verification and outstanding work

- [x] Host inventory executes in the existing Mac runtime.
- [x] Stationary probe: 21 offline tests pass, including malformed telemetry,
  early video delivery, timeouts, disconnect failure, and omission of vendor
  secrets. Tests use fake connections and never dial hardware.
- [x] Contract schema export check and frontend JavaScript syntax check pass.
- [ ] Complete the app's initial Wi-Fi setup and discover the actual robot IP.
- [ ] Run the stationary probe on the physical robot and record firmware.
- [ ] Verify the GX10 runtime, local image inference and USB audio.
- [ ] Connect synchronized hardware evidence to the app and rehearse bounded motion.

The broader app test run during this milestone had 79 passes and 18 failures
in the concurrently edited delivery/recovery tests. This probe changes no app
policy; those failures remain separate integration work. No software test here
is evidence of a physical camera frame or audible output.
