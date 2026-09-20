"""Create indexes and seed empty collections in the configured database."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.database import Database, COLLECTIONS
from app.seed import seed


async def main():
    database = Database(Settings())
    try:
        await database.connect()
        await seed(database)
        counts = {name: await database.db[name].count_documents({}) for name in COLLECTIONS}
        print('MongoDB initialized. Collection counts:', counts)
        return 0
    except Exception as exc:
        print(f'Initialization failed ({type(exc).__name__}); connection details withheld.')
        return 1
    finally:
        await database.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
