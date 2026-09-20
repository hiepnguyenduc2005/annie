"""Start the API against configured MongoDB; verify reads without dispatching robot work."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import Settings
from app.main import create_app


async def main():
    settings = Settings(worker_enabled=False, robot_dispatch_enabled=False, reminder_scheduler_enabled=False)
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                for path in ('/health', '/ready', '/api/dog-users', '/api/app-users?dog_user_id=1',
                             '/api/reminders?app_user_id=2', '/api/reminders?app_user_id=3',
                             '/api/messages?app_user_id=2', '/api/history?app_user_id=2', '/openapi.json'):
                    response = await client.get(path)
                    if response.status_code != 200:
                        print(f'API check failed: {path} HTTP {response.status_code}')
                        return 1
                    print(f'OK {path}')
        print('Configured MongoDB API startup/read checks passed; robot dispatch disabled.')
        return 0
    except Exception as exc:
        print(f'API check failed ({type(exc).__name__}); connection details withheld.')
        return 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
