# Local Unitree joystick control fixes

These patches target the local [unitree_ui](https://github.com/legion1581/unitree_ui)
checkout at revision `3eb378b7adcf06a7773724efcbb4f58a8df98e11` (MIT;
see [upstream license](UPSTREAM-LICENSE)). They were applied to
`.cache/unitree-ui` on 2026-09-19. The cached checkout is ignored by Annie;
these files preserve the focused changes for reconstruction.

`control-stop.patch` changes only `src/ui/app.ts` and
`src/ui/components/joystick.ts`:

- Bluetooth stick release forwards neutral input.
- Losing window focus, hiding/unloading the page, leaving the controls, or
  disconnecting cancels input producers and clears held state before sending
  neutral and a priority Go2 StopMove request. Queued old callbacks cannot
  overwrite that stop. Returning focus alone does not restart driving.
- Fresh joystick interaction or explicit controller selection resumes input.
- The red emergency control retains Damp, which relaxes the motors, and its
  tooltip/accessibility label distinguishes it from normal walking stops.
- The hub labels the controls **Drive / Joysticks**.

Stop delivery is best effort. A disconnected or failed host cannot deliver
these packets; passing the tests below does not prove robot-side stopping,
loss-link behavior, a hardware emergency stop, or a successful physical walk.
No test connects to a robot or opens a browser/network client.

## Apply and verify

From the Annie repository root, with the pinned UI checkout and its existing
Node dependencies present:

```sh
git -C .cache/unitree-ui apply --check ../../robot/tooling/unitree-ui/control-stop.patch
git -C .cache/unitree-ui apply ../../robot/tooling/unitree-ui/control-stop.patch
mkdir -p .cache/unitree-ui/tests
cp robot/tooling/unitree-ui/motion-stop.test.mjs .cache/unitree-ui/tests/
cd .cache/unitree-ui
node --test tests/motion-stop.test.mjs
npm run build
```

The patch is already applied in the working cache. A reverse apply check can
confirm that state; do not reapply or discard unrelated UI changes to make a
patch fit. `connection-help.patch` is maintained separately by the hardware
owner and is not part of the stop-control patch.

The regression harness transpiles the actual production App and Joystick
methods with in-memory DOM, timers and transport fakes. It does not instantiate
the App constructor that connects to external services. All 16 checks passed
on the patched source, covering neutral release, lifecycle stop, producer
cancellation, source changes, stale callbacks, transport failure, disconnect
ordering, emergency damping, fresh manual restart and G1 API separation.
TypeScript checking passed; the hardware owner also verified the combined
Vite build. Physical acceptance remains separate.
