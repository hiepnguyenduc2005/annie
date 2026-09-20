"""Initialize only the dedicated localhost test replica set."""
import time
from pymongo import MongoClient
from pymongo.errors import OperationFailure

client = MongoClient('mongodb://127.0.0.1:27029/?directConnection=true', serverSelectionTimeoutMS=3000)
try:
    client.admin.command('replSetInitiate', {'_id': 'annieTest', 'members': [{'_id': 0, 'host': '127.0.0.1:27029'}]})
except OperationFailure as exc:
    if exc.code != 23:
        raise
for _ in range(30):
    if client.admin.command('hello').get('isWritablePrimary'):
        print('Isolated MongoDB test replica set ready on port 27029.')
        break
    time.sleep(.5)
else:
    raise RuntimeError('Test replica set not ready')
client.close()
