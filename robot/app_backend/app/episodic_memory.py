"""Spatiotemporal episodic memory retrieval over stored perception frames.

Grounded in ReMEmbR (https://github.com/NVIDIA-AI-IOT/remembr): captions bound
to capture time and robot pose, retrieved by temporal and spatial relevance.
This implementation is deterministic: lexical entity matching, waypoint
proximity, and time windows replace ReMEmbR's LLM planner, so every answer
cites stored frames and unsupported questions are refused rather than inferred.

Reads the existing bounded memory table written by Service.observe; it never
writes frames and stores no raw images.
"""
import json
import re
import time
from math import hypot

MAX_FRAMES = 1000  # Mirrors the bounded store in Service.observe.
FUTURE_SKEW_MS = 0  # Future-dated frames are prohibited, never evidence.
DAY_MS = 86_400_000

STOP = {
    'where', 'when', 'was', 'were', 'the', 'a', 'an', 'is', 'are', 'did', 'do', 'does',
    'i', 'my', 'me', 'you', 'see', 'saw', 'last', 'what', 'for', 'of', 'in', 'at', 'to',
    'near', 'how', 'long', 'there', 'that', 'it', 'its', 'on', 'by', 'and', 'with',
    'have', 'has', 'been', 'any', 'about', 'tell',
}

QUESTION_WORDS = {'how', 'long', 'ago', 'visible', 'seen', 'saw', 'present', 'appear',
                  'appeared', 'show', 'shown', 'spotted'}

UNITS_MS = {'second': 1000, 'minute': 60_000, 'hour': 3_600_000, 'day': DAY_MS}
TIME_TOKENS = {unit + suffix for unit in UNITS_MS for suffix in ('', 's')} | {'today'}


def _single(token):
    # Bounded normalization: a simple trailing-s singular applied to both query
    # and caption tokens, so plural forms match without fuzzy guessing.
    return token[:-1] if len(token) > 3 and token.endswith('s') else token


def _utc(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(ts / 1000))


def _age(ms):
    if ms < 60_000:
        return f'{ms // 1000}s'
    if ms < 3_600_000:
        return f'{ms // 60_000} min'
    if ms < DAY_MS:
        return f'{ms // 3_600_000} h'
    return f'{ms // DAY_MS} d'


class EpisodicMemory:
    """Where/when retrieval with evidence over an existing SQLite memory table."""

    def __init__(self, db, now=None, map_id=None, waypoints=(), radius_m=2.5):
        # Frame timestamps are milliseconds; seconds clocks must convert.
        self.now = now if now is not None else (lambda: int(time.time() * 1000))
        self.db = db
        self.map_id = map_id
        self.waypoints = list(waypoints)
        self.radius_m = radius_m

    def ask(self, text, now=None):
        now = int(self.now() if now is None else now)
        norm = ' '.join(re.findall(r'\w+', text.lower()))
        tokens = set(norm.split())
        if {'who', 'whose'} & tokens:
            return self._refuse('I can\'t identify people. I only store camera captions with time and pose.', 'identity_refused')
        frames, excluded = self._load(now)
        window = self._window_ms(norm, tokens, now)
        mentioned = self._mentioned_waypoints(norm)
        waypoint_tokens = set()
        for waypoint in mentioned:
            waypoint_tokens |= {str(waypoint.get('id', '')).replace('-', ' '),
                                str(waypoint.get('label', ''))} - {''}
        waypoint_tokens = {piece for phrase in waypoint_tokens for piece in phrase.split()}
        # Waypoint mentions, time expressions, and bare numbers are query
        # constraints; only remaining tokens select content.
        # "how long" is a duration question, not a caption phrase; drop it so
        # the remaining content tokens select the entity.
        duration_words = QUESTION_WORDS if {'how', 'long'} <= tokens else set()
        content = (tokens - STOP - waypoint_tokens - TIME_TOKENS - QUESTION_WORDS
                   - {t for t in tokens if t.isdigit()} - duration_words)
        required = {_single(token) for token in content}
        positive_person = 'person' in required and not {'no', 'not', 'without'} & tokens
        caption_required = required - {'person'} if positive_person else required
        scored = []
        for item in frames:
            # Negated captions contain the word "person" too. Presence queries
            # must agree with the stored perception, not a lexical overlap.
            if positive_person and item.get('person') is not True:
                continue
            caption_tokens = {_single(token) for token in re.findall(r'\w+', item['caption'].lower())}
            if caption_required and caption_required - caption_tokens:
                # Every meaningful content token must match. Partial overlaps
                # ("blue umbrella" vs "blue cup") are false hits, not answers.
                continue
            near = bool(mentioned) and any(
                (self.map_id is None or item['pose']['map_id'] == self.map_id)
                and hypot(item['pose']['x'] - w['x'], item['pose']['y'] - w['y']) <= self.radius_m
                for w in mentioned)
            # A waypoint filter must hold on the current map; coordinates from
            # an unrelated old map do not satisfy a spatial question.
            if mentioned and not near:
                continue
            if window is not None and not window[0] <= item['ts'] <= window[1]:
                continue
            scored.append({'frame': item, 'score': len(required) + (1 if near else 0)})
        if not scored:
            if mentioned:
                place = mentioned[0].get('id', 'asked waypoint')
                return self._refuse(
                    f'I have no matching observation near the {place} on the current map.',
                    'not_near', excluded)
            return self._refuse('I wasn\u2019t there for that', 'no_evidence', excluded)
        scored.sort(key=lambda pair: (pair['score'], pair['frame']['ts']), reverse=True)
        if 'how' in tokens and 'long' in tokens:
            return self._duration(scored, now, excluded)
        if 'when' in tokens or 'last' in norm:
            return self._when(scored, now, excluded)
        if 'where' in tokens:
            return self._where(scored, now, mentioned, excluded)
        return self._lookup(scored, now, excluded)

    def _load(self, now):
        frames, excluded = [], 0
        rows = self.db.execute(
            'SELECT payload FROM memory ORDER BY ts DESC, rowid DESC LIMIT ?', (MAX_FRAMES,))
        for (payload,) in rows:
            item = json.loads(payload)
            if item['ts'] > now + FUTURE_SKEW_MS:
                # Future-dated frames are capture anomalies, never evidence.
                excluded += 1
                continue
            frames.append(item)
        return frames, excluded

    def _window_ms(self, norm, tokens, now):
        match = re.search(r'last (\d+) (second|minute|hour|day)s?', norm)
        if match:
            span = int(match.group(1)) * UNITS_MS[match.group(2)]
            return (now - span, now)
        if 'today' in tokens:
            return (now - now % DAY_MS, now)
        return None

    def _mentioned_waypoints(self, norm):
        found = []
        for waypoint in self.waypoints:
            phrases = {str(waypoint.get('id', '')).replace('-', ' ').lower(),
                       str(waypoint.get('label', '')).lower()} - {''}
            if any(phrase in norm for phrase in phrases):
                found.append(waypoint)
        return found

    def _citations(self, scored, now, limit=3):
        return [self._cite(pair['frame'], now) for pair in scored[:limit]]

    def _cite(self, item, now):
        pose = item['pose']
        return {'frame_id': item['frame_id'], 'ts': item['ts'], 'pose': pose, 'crop_url': None,
                'caption': item['caption'], 'source': item.get('source', 'mock'),
                'model': item.get('model'),
                'map_current': None if self.map_id is None else pose['map_id'] == self.map_id,
                'historical': now - item['ts'] > DAY_MS}

    def _refuse(self, answer, kind, excluded=0):
        return {'answerable': False, 'kind': kind, 'answer': answer, 'citations': [],
                'excluded_future': excluded}

    def _duration(self, scored, now, excluded):
        ordered = sorted(scored, key=lambda pair: pair['frame']['ts'])
        if len(ordered) < 2:
            # One capture cannot establish a duration; say so instead of
            # reporting an elapsed window that implies continuous presence.
            only = ordered[0]['frame']
            return {'answerable': False, 'kind': 'duration_unknown',
                    'answer': f'Only one matching capture, at {_utc(only["ts"])}; I can\'t establish how long.',
                    'citations': [self._cite(only, now)], 'excluded_future': excluded}
        first, last = ordered[0]['frame'], ordered[-1]['frame']
        span = last['ts'] - first['ts']
        answer = (f'Matching captures at {_utc(first["ts"])} and {_utc(last["ts"])}; '
                  f'elapsed between captures {_age(span)}; continuous presence not established.')
        return {'answerable': True, 'kind': 'duration', 'answer': answer,
                'citations': [self._cite(first, now), self._cite(last, now)],
                'excluded_future': excluded}

    def _when(self, scored, now, excluded):
        # "Last" means the latest capture among eligible matches, regardless of
        # which match scored highest lexically.
        ranked = sorted(scored, key=lambda pair: pair['frame']['ts'], reverse=True)
        item = ranked[0]['frame']
        answer = f'Last seen {_utc(item["ts"])} ({_age(now - item["ts"])} ago): {item["caption"]}'
        return {'answerable': True, 'kind': 'last_seen', 'answer': answer,
                'citations': self._citations(ranked, now), 'excluded_future': excluded}

    def _where(self, scored, now, mentioned, excluded):
        # Relevance first; the current map only breaks ties.
        ranked = sorted(scored, key=lambda pair: (
            pair['score'],
            self.map_id is not None and pair['frame']['pose']['map_id'] == self.map_id,
            pair['frame']['ts']), reverse=True)
        best = ranked[0]
        item = best['frame']
        pose = item['pose']
        place = f'x={pose["x"]:g}, y={pose["y"]:g}, map {pose["map_id"]}'
        # The pose locates the observing robot; the caption describes the scene.
        answer = f'At {_utc(item["ts"])} ({_age(now - item["ts"])} ago), from pose ({place}), I observed: {item["caption"]}.'
        if mentioned:
            nearest = min((hypot(pose['x'] - w['x'], pose['y'] - w['y']), w) for w in mentioned)
            if nearest[0] > self.radius_m:
                answer = f'Not near the {nearest[1].get("id", "asked waypoint")}; ' + answer
        if self.map_id is not None and pose['map_id'] != self.map_id:
            answer += f' Note: that was on map {pose["map_id"]}, not the current map {self.map_id}.'
        return {'answerable': True, 'kind': 'where', 'answer': answer,
                'citations': self._citations(ranked, now), 'excluded_future': excluded}

    def _lookup(self, scored, now, excluded):
        answer = ' '.join(pair['frame']['caption'] for pair in scored[:3])
        return {'answerable': True, 'kind': 'lookup', 'answer': answer,
                'citations': self._citations(scored, now), 'excluded_future': excluded}
