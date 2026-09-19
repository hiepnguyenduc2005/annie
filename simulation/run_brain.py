"""Start the configurable brain using ignored root .env credentials.

Local Ollama vision is the default; starting this service makes no paid call
and cloud requests still reserve the shared budget before external inference.
A cloud run (--mode cloud) requires an explicit --model.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from dotenv import load_dotenv
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['cloud', 'local'], default='local')
    parser.add_argument('--model')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--cloud-audio', action='store_true',
                        help='Enable explicit synthetic WAV transcription through MiMo/OpenRouter')
    args = parser.parse_args()
    load_dotenv(root / '.env', override=False)
    os.environ['ANNIE_AUDIO_ENABLED'] = 'true' if args.cloud_audio else 'false'
    os.environ['ANNIE_VISION_MODE'] = args.mode
    if args.mode == 'cloud':
        if not args.model:
            parser.error('--mode cloud requires an explicit --model (no automatic default or fallback)')
        os.environ['ANNIE_VISION_BASE_URL'] = 'https://openrouter.ai/api/v1'
        os.environ['ANNIE_VISION_API_KEY'] = os.getenv('OPENROUTER_API_KEY', '')
        os.environ['ANNIE_VISION_MODEL'] = args.model
    else:
        os.environ['ANNIE_VISION_BASE_URL'] = 'http://127.0.0.1:11434/v1'
        os.environ['ANNIE_VISION_API_KEY'] = ''
        os.environ['ANNIE_VISION_MODEL'] = args.model or 'qwen3-vl:2b-instruct'
    uvicorn.run('robot_backend.app.brain.api:app', host='127.0.0.1', port=args.port,
                proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
