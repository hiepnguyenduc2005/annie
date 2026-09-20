"""Bounded exact-frame evidence for synthetic camera inference only."""
import base64
import json
from pathlib import Path
from uuid import UUID


def save_evidence(directory, frame, perception, *, limit=64):
    if frame.get('source') != 'simulation_render' or perception.get('source') != 'simulation_vlm':
        raise ValueError('Only synthetic image inference is retained here')
    if any(frame.get(k) != perception.get(k) for k in ('frame_id', 'ts', 'pose')):
        raise ValueError('Evidence identity mismatch')
    identity = str(UUID(frame['frame_id']))
    raw = base64.b64decode(frame['jpeg_b64'], validate=True)
    if len(raw) > 1_000_000 or not raw.startswith(b'\xff\xd8'):
        raise ValueError('Invalid bounded JPEG evidence')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    image = root / (identity+'.jpg')
    image.write_bytes(raw)
    image.with_suffix('.json').write_text(json.dumps(perception, indent=2)+'\n')
    # This directory belongs solely to the synthetic evidence cache.
    files = sorted(root.glob('*.jpg'), key=lambda p: p.stat().st_mtime)
    for old in files[:-limit]:
        old.unlink()
        old.with_suffix('.json').unlink(missing_ok=True)
    return str(image)
