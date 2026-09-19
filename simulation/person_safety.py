"""Latest-frame camera interlock; a demo software stop, never a hardware e-stop."""
from __future__ import annotations

import threading
import time


class PersonSafety:
    def __init__(self, detector_factory=None, *, clock=time.time, start=True):
        self.clock = clock
        self.condition = threading.Condition()
        self.map_id = None
        self.pending = self.result = None
        self.error = None
        self.latched = False
        self.closed = False
        self.detector_factory = detector_factory or self.default_detector
        if start:
            threading.Thread(target=self.worker, daemon=True, name='camera-person-stop').start()

    @staticmethod
    def default_detector():
        from simulation.local_perception import PersonDetector, default_checkpoint
        # CPU avoids competing with the local VLM's Apple GPU allocation.
        return PersonDetector(default_checkpoint(), device='cpu')

    def reset(self, map_id):
        with self.condition:
            self.map_id = map_id
            self.pending = self.result = None
            self.error = None
            self.latched = False

    def submit(self, observation):
        with self.condition:
            if observation['pose']['map_id'] == self.map_id:
                self.pending = observation  # Replace, never accumulate frames.
                self.condition.notify()

    def record(self, observation, result):
        with self.condition:
            if observation['pose']['map_id'] != self.map_id:
                return
            if result['frame_id'] != observation['frame_id']:
                self.error = 'Detector returned a different capture identity'
                return
            self.error = None
            self.result = {**result, 'captured_at': observation['ts'], 'map_id': self.map_id}
            if result['stop_recommended']:
                self.latched = True

    def snapshot(self):
        with self.condition:
            r = self.result or {}
            fresh = bool(r) and 0 <= self.clock() * 1000 - r['captured_at'] <= 1000
            reason = self.error or ('Camera detector warming up' if not r else
                     'Camera detector result is stale' if not fresh else
                     'Person detected — movement inhibited' if r['stop_recommended'] else
                     'Stop latched — issue a new mission after the view is clear' if self.latched else
                     'Fresh camera result; no person detected')
            return {'enabled': True, 'ready': fresh and not self.error,
                    'blocked': not fresh or bool(self.error) or self.latched,
                    'reason': reason, 'detections': r.get('persons', []),
                    'frame_id': r.get('frame_id'), 'captured_at': r.get('captured_at'),
                    'latency_ms': r.get('latency_ms'), 'model': r.get('model_path', 'yolo11s.pt'),
                    'source': 'robot_camera_yolo', 'threshold': 0.4}

    def explicit_restart(self):
        """A clear, fresh view plus a new operator mission can release the latch."""
        with self.condition:
            if self.result and not self.result['stop_recommended']:
                if 0 <= self.clock() * 1000 - self.result['captured_at'] <= 1000 and not self.error:
                    self.latched = False

    def worker(self):
        try:
            detector = self.detector_factory()
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending is not None or self.closed)
                    if self.closed:
                        return
                    observation, self.pending = self.pending, None
                try:
                    result = detector.detect_observation(observation).as_dict()
                    self.record(observation, result)
                except Exception:
                    with self.condition:
                        if observation['pose']['map_id'] == self.map_id:
                            self.error = 'Person detector unavailable; movement inhibited'
        except Exception:
            with self.condition:
                self.error = 'Person detector failed to load; movement inhibited'

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()
