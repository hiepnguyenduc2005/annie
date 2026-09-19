"""Export deterministic channel contracts: python contract/export_schemas.py [--check]."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app_backend'))
from app.models import CHANNEL_MODELS, Event

path = Path(__file__).with_name('schemas.json')
schemas = {name: model.model_json_schema() for name, model in {**CHANNEL_MODELS, 'event': Event}.items()}
output = json.dumps(schemas, indent=2, sort_keys=True) + '\n'
parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
if args.check:
    if not path.exists() or path.read_text() != output:
        raise SystemExit('Contract schemas are out of date')
else:
    path.write_text(output)
