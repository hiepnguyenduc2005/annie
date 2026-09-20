"""Read-only connection check; never prints the URI or provider exception."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from pymongo import AsyncMongoClient


async def main():
    settings = Settings()
    uri = settings.mongodb_uri.get_secret_value()
    if not uri:
        print('MONGODB_URI is missing.')
        return 1
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        hello = await client.admin.command('hello')
        if not hello.get('setName') and hello.get('msg') != 'isdbgrid':
            print('Connected, but transactions require Atlas or a replica set.')
            return 1
        print('MongoDB reachable; transaction-capable deployment confirmed.')
        return 0
    except Exception as exc:
        print(f'MongoDB connection failed ({type(exc).__name__}); URI withheld.')
        return 1
    finally:
        await client.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
