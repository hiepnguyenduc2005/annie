"""Low-latency perception for the live dog: convert -> track -> annotate, one thread per stage, with diagnostics.

Why a pipeline: on the event loop, every frame waited on every other job (WebRTC receive,
telemetry, LiDAR decode, the control tick) and the measured result was 1-2 processed
frames per second with 400+ frames received. Here the newest frame is handed to a
converter thread (video frame -> small BGR array), the tracker thread runs pose +
ByteTrack on it (the only stateful stage, so it is never run concurrently), and an
annotator thread draws boxes/skeletons and publishes the JPEG for the live view. Old
frames are dropped at every hop: the control loop always acts on the newest result.

`Diag` counts what actually happened: received fps, processed fps, per-stage ms (mean
and p95), frame age at the moment tracks are published, and dropped frames. Print it,
read it from `/status.json`, or compare bounded runs with different settings.

`plausible_people` filters hallucinated detections before any behaviour acts on them: a
minimum confidence, a minimum number of confident keypoints (a chair does not have a
skeleton), and a minimum track age so a one-frame flicker never steers the dog.
"""
from __future__ import annotations

import queue
import statistics
import threading
import time


class Diag:
    """Rolling counters and stage timings; thread-safe enough for one writer per stage."""

    def __init__(self, window_s=3.0, keep=300):
        self.window_s, self.keep = window_s, keep
        self.lock = threading.Lock()
        self.stamps = {"received": [], "processed": [], "control": [], "safety": [], "motion_tx": []}
        self.samples = {"convert_ms": [], "track_ms": [], "annotate_ms": [], "age_ms": [], "loop_lag_ms": [],
                        "voxel_ms": [], "queue_ms": []}
        self.counts = {"received": 0, "processed": 0, "dropped": 0, "errors": 0}

    def tick(self, name, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            lst = self.stamps.setdefault(name, [])
            lst.append(now)
            if len(lst) > 2000:
                del lst[: len(lst) - 2000]
            if name in self.counts:
                self.counts[name] += 1

    def count(self, name, n=1):
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + n

    def sample(self, name, value_ms):
        with self.lock:
            lst = self.samples.setdefault(name, [])
            lst.append(float(value_ms))
            if len(lst) > self.keep:
                del lst[: len(lst) - self.keep]

    def rate(self, name, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            lst = self.stamps.get(name, [])
            recent = [t for t in lst if now - t <= self.window_s]
        return len(recent) / self.window_s

    def snapshot(self):
        now = time.monotonic()
        out = {"received_fps": round(self.rate("received", now), 1), "processed_fps": round(self.rate("processed", now), 1),
               "control_hz": round(self.rate("control", now), 1),
               "safety_hz": round(self.rate("safety", now), 1),
               "motion_tx_hz": round(self.rate("motion_tx", now), 1)}
        with self.lock:
            # control_hz is the outer behaviour loop, which pauses for speech/tricks.
            # Count actual guard evaluations separately; a heartbeat is not a safety check.
            guards = self.stamps["safety"]
            age = max(0.0, now - guards[-1]) if guards else None
            gaps = [b - a for a, b in zip(guards, guards[1:]) if now - b <= self.window_s]
            out["safety_age_ms"] = None if age is None else round(age * 1000, 1)
            out["safety_max_gap_ms"] = None if age is None else round(max([age, *gaps]) * 1000, 1)
            out.update(self.counts)
            for name, lst in self.samples.items():
                if lst:
                    recent = lst[-60:]
                    srt = sorted(recent)
                    out[name] = {"mean": round(statistics.fmean(recent), 1), "p95": round(srt[int(0.95 * (len(srt) - 1))], 1)}
        return out

    def line(self):
        s = self.snapshot()

        def ms(k):
            v = s.get(k)
            return "-" if not v else f"{v['mean']:.0f}/{v['p95']:.0f}"
        return (f"recv={s['received_fps']:.1f}fps proc={s['processed_fps']:.1f}fps behaviour={s['control_hz']:.1f}Hz "
                f"safety={s['safety_hz']:.1f}Hz gap={s['safety_max_gap_ms']}ms "
                f"age={ms('age_ms')}ms conv={ms('convert_ms')} track={ms('track_ms')} ann={ms('annotate_ms')} "
                f"loop_lag={ms('loop_lag_ms')} voxel={ms('voxel_ms')} dropped={s['dropped']} errors={s['errors']}")


def _put_latest(q: queue.Queue, item, diag: Diag | None = None):
    """Keep only the newest item in a size-1 queue."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
                if diag:
                    diag.count("dropped")
            except queue.Empty:
                pass


def plausible_people(tracks, *, now_ms, min_conf=0.45, min_keypoints=4, kp_conf=0.3, min_age_ms=250):
    """Drop detections that do not look like a person yet: low confidence, no skeleton, or too new."""
    out = []
    for t in tracks:
        if t.get("conf", 0.0) < min_conf:
            continue
        kpc = t.get("kp_conf") or []
        if kpc and sum(1 for c in kpc if c >= kp_conf) < min_keypoints:
            continue
        first = t.get("first_seen_ms")
        if first is not None and now_ms - first < min_age_ms:
            continue
        out.append(t)
    return out


class Perception:
    """Three worker threads; `submit` from the receiver, `latest()` from the control loop."""

    def __init__(self, tracker, *, convert, annotate=None, diag: Diag | None = None, min_conf=0.45,
                 min_keypoints=4, min_age_ms=250, identifier=None, objects=None, objects_every=5):
        self.tracker, self.convert, self.annotate = tracker, convert, annotate
        self.identifier = identifier  # e.g. go2_target_id.TargetIdentifier: names tracks by shirt colour
        self.objects, self.objects_every = objects, objects_every  # optional ObjectDetector, run every Nth frame
        self.diag = diag or Diag()
        self.filter = dict(min_conf=min_conf, min_keypoints=min_keypoints, min_age_ms=min_age_ms)
        self.in_q: queue.Queue = queue.Queue(maxsize=1)
        self.track_q: queue.Queue = queue.Queue(maxsize=1)
        self.ann_q: queue.Queue = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.result = {"seq": 0, "tracks": [], "raw_tracks": [], "objects": [], "objects_seq": 0, "w": None, "h": None, "t": None}
        self.context = {"mode": "-", "ranges": None, "battery": None}
        self.stopped = False
        self.threads = [threading.Thread(target=fn, daemon=True, name=name)
                        for name, fn in (("convert", self._convert_loop), ("track", self._track_loop),
                                         ("annotate", self._annotate_loop))]

    def start(self):
        for t in self.threads:
            t.start()
        return self

    def stop(self):
        self.stopped = True

    def submit(self, frame, t_recv=None):
        """Receiver side: newest frame wins."""
        self.diag.tick("received")
        _put_latest(self.in_q, (frame, t_recv or time.monotonic()), self.diag)

    def set_context(self, **fields):
        with self.lock:
            self.context.update(fields)

    def latest(self):
        with self.lock:
            return dict(self.result)

    def _convert_loop(self):
        while not self.stopped:
            try:
                frame, t_recv = self.in_q.get(timeout=0.5)
            except queue.Empty:
                continue
            t0 = time.perf_counter()
            try:
                img = self.convert(frame)
            except Exception:
                self.diag.count("errors")
                continue
            self.diag.sample("convert_ms", (time.perf_counter() - t0) * 1000)
            _put_latest(self.track_q, (img, t_recv), self.diag)

    def _track_loop(self):
        while not self.stopped:
            try:
                img, t_recv = self.track_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.diag.sample("queue_ms", (time.monotonic() - t_recv) * 1000)
            t0 = time.perf_counter()
            now_ms = int(time.time() * 1000)
            try:
                raw = self.tracker.update(img, now_ms=now_ms)
            except Exception:
                self.diag.count("errors")
                continue
            self.diag.sample("track_ms", (time.perf_counter() - t0) * 1000)
            tracks = plausible_people(raw, now_ms=now_ms, **self.filter)
            if self.identifier is not None and tracks:
                try:
                    self.identifier.apply(img, tracks)
                except Exception:
                    self.diag.count("errors")
            objects, objects_seq = self.result["objects"], self.result["objects_seq"]
            if self.objects is not None and self.result["seq"] % self.objects_every == 0:
                t1 = time.perf_counter()
                try:
                    objects = self.objects.detect(img, now_ms)
                    objects_seq += 1  # a fresh list: consumers record it once, never re-project a stale one
                except Exception:
                    self.diag.count("errors")
                self.diag.sample("objects_ms", (time.perf_counter() - t1) * 1000)
            now = time.monotonic()
            with self.lock:
                self.result = {"seq": self.result["seq"] + 1, "tracks": tracks, "raw_tracks": raw, "objects": objects,
                               "objects_seq": objects_seq, "w": img.shape[1], "h": img.shape[0], "t": now}
                ctx = dict(self.context)
                ctx["objects"] = objects
            self.diag.tick("processed", now)
            self.diag.sample("age_ms", (now - t_recv) * 1000)
            if self.annotate is not None:
                _put_latest(self.ann_q, (img, tracks, raw, ctx))

    def _annotate_loop(self):
        while not self.stopped:
            try:
                img, tracks, raw, ctx = self.ann_q.get(timeout=0.5)
            except queue.Empty:
                continue
            t0 = time.perf_counter()
            try:
                self.annotate(img, tracks, raw, ctx)
            except Exception:
                self.diag.count("errors")
                continue
            self.diag.sample("annotate_ms", (time.perf_counter() - t0) * 1000)
