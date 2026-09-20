"""Export the v2 OpenAPI contract and document schemas without connecting to MongoDB."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import create_app
from app.config import Settings
from app.schemas.users import DogUser, AppUser
from app.schemas.reminders import Reminder, Note
from app.schemas.messages import Conversation, MessageEntry
from app.schemas.notifications import Notification

root = Path(__file__).resolve().parents[1] / 'contract'
artifacts = {'openapi.json': create_app(Settings(_env_file=None)).openapi()}
for model in (DogUser, AppUser, Reminder, Note, Conversation, MessageEntry, Notification):
    artifacts[model.__name__ + '.schema.json'] = model.model_json_schema()
parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
for name, value in artifacts.items():
    text = json.dumps(value, indent=2, sort_keys=True) + '\n'
    path = root / name
    if args.check:
        if not path.exists() or path.read_text() != text:
            raise SystemExit(f'Stale schema: {name}')
    else:
        root.mkdir(exist_ok=True)
        path.write_text(text)
print('Contract schemas are current.')
