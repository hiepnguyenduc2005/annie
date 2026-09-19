"""Fetch pinned person-detector weights and warm local speech recognition."""
import hashlib
from pathlib import Path
import urllib.request

URL = 'https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt'
SHA256 = '85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5'


def main():
    root = Path(__file__).resolve().parents[2]
    target = root / '.cache/yolo/yolo11s.pt'
    blob = target.read_bytes() if target.exists() else b''
    if hashlib.sha256(blob).hexdigest() != SHA256:
        with urllib.request.urlopen(URL, timeout=60) as response:
            blob = response.read(30_000_001)
        if len(blob) > 30_000_000 or hashlib.sha256(blob).hexdigest() != SHA256:
            raise ValueError('Person checkpoint size or hash mismatch')
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix('.part')
        part.write_bytes(blob)
        part.replace(target)
    print('YOLO11s checkpoint verified:', SHA256)
    from local_stt import LocalSTTAdapter
    LocalSTTAdapter(cache_dir=root / '.cache/models/whisper').warm()
    print('Whisper tiny.en ready locally')


if __name__ == '__main__':
    main()
