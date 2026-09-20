"""Pure completion checks for speech issued by one simulation goal.

The caller records command IDs with their issuing map and goal revision.
Global command history alone cannot identify a goal's speech.
"""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class IssuedSpeech:
    command_id: str
    map_id: str
    goal_revision: int


class SpeechReceiptCache:
    """Keep observed delivery results for issued speech beyond app pagination.

    Only current-goal IDs are retained, and only a verified app completion or
    observed failure survives eviction. A contradictory pending snapshot
    invalidates earlier success; absence alone never creates success.
    """

    def __init__(self):
        self.scope = None
        self.receipts = {}

    def clear(self):
        self.scope = None
        self.receipts.clear()

    def merge(self, issued_speech, command_receipts, speech_clips=(), *, map_id, goal_revision):
        scope = (map_id, goal_revision)
        if self.scope != scope:
            self.clear()
            self.scope = scope
        issued = {item.command_id: item for item in issued_speech
                  if (item.map_id, item.goal_revision) == scope}
        self.receipts = {cid: item for cid, item in self.receipts.items() if cid in issued}
        commands, clips = tuple(command_receipts), tuple(speech_clips)
        for cid, owner in issued.items():
            latest = [item for item in commands if item.get('command_id') == cid]
            cached = self.receipts.get(cid)
            evidence = latest or ([cached] if cached else [])
            # A failed required delivery cannot become a success merely because
            # a later snapshot changes; a new goal establishes new requirements.
            if latest and cached and cached['status'] == 'failed':
                evidence = latest + [cached]
            result = goal_speech_completion([owner], evidence, clips,
                map_id=map_id, goal_revision=goal_revision)
            if result.state in ('completed', 'failed'):
                self.receipts[cid] = {'command_id': cid, 'cmd': 'say', 'status': result.state}
            else:
                self.receipts.pop(cid, None)
        # Include all current duplicates: cache normalization must not hide a
        # pending, failed, or wrong-command row in the authoritative snapshot.
        return [dict(item) for item in self.receipts.values()] + [
            item for item in commands if item.get('command_id') in issued]


@dataclass(frozen=True)
class GoalSpeechCompletion:
    state: Literal["not_required", "pending", "failed", "completed"]
    pending_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()

    @property
    def allows_completion(self) -> bool:
        return self.state in {"not_required", "completed"}

    @property
    def delivery_verified(self) -> bool:
        return self.state == "completed"

    @property
    def detail(self) -> str:
        return {
            "not_required": "This goal has no required speech commands",
            "pending": "Current-goal speech needs completed playback receipts",
            "failed": "Current-goal speech delivery failed",
            "completed": "Current-goal speech playback receipts completed",
        }[self.state]


def goal_speech_completion(
    issued_speech: Iterable[IssuedSpeech],
    command_receipts: Iterable[Mapping[str, object]],
    speech_clips: Iterable[Mapping[str, object]] = (),
    *,
    map_id: str,
    goal_revision: int,
    require_speech: bool = False,
) -> GoalSpeechCompletion:
    """Check only the caller's current-goal speech IDs, without choosing actions.

    App ``say/completed`` receipts represent completed playback, not synthesis.
    Matching viewer clips, when retained, must agree: only ``played`` confirms
    playback. Missing clips may have aged out of the viewer; they do not erase
    an identified app completion receipt. Missing/unknown command receipts
    remain pending. Duplicate records cannot hide a failure or pending state.

    ``require_speech`` prevents a delivery goal from finishing without issuing
    speech. An observation-only goal can finish with no speech, but this does
    not produce delivery evidence. Failed required attempts stay failed; the
    caller must explicitly establish a new goal or a superseding requirement,
    rather than treating any later, possibly unrelated clip as a retry.
    """
    if not isinstance(map_id, str) or not map_id.strip():
        raise ValueError("Current goal needs a nonempty map ID")
    if type(goal_revision) is not int or goal_revision < 0:
        raise ValueError("Current goal needs a nonnegative integer revision")
    ids = tuple(item.command_id for item in issued_speech
                if item.map_id == map_id and item.goal_revision == goal_revision)
    if any(not isinstance(cid, str) or not cid.strip() for cid in ids):
        raise ValueError("Current-goal speech IDs must be nonempty strings")
    ids = tuple(dict.fromkeys(ids))
    if not ids:
        return GoalSpeechCompletion("pending" if require_speech else "not_required")

    commands = tuple(command_receipts)
    clips = tuple(speech_clips)
    pending, failed = [], []
    for cid in ids:
        receipts = [item for item in commands if item.get("command_id") == cid]
        matched_clips = [item for item in clips if item.get("command_id") == cid]
        if any(item.get("status") == "failed" for item in receipts + matched_clips):
            failed.append(cid)
        elif (not receipts
              or any(item.get("cmd") != "say" or item.get("status") != "completed"
                     for item in receipts)
              or any(item.get("status") != "played" for item in matched_clips)):
            pending.append(cid)

    state = "failed" if failed else "pending" if pending else "completed"
    return GoalSpeechCompletion(state, tuple(pending), tuple(failed))
