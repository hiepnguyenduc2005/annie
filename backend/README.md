# Annie backend

FastAPI backend. Requires Python 3.10 or newer.

## Setup

From the repository root:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run locally

From `backend`, with the virtual environment activated:

```sh
fastapi dev app/main.py
```

- API: http://127.0.0.1:8000
- Interactive documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

The development server reloads automatically when application files change.
See the [FastAPI documentation](https://fastapi.tiangolo.com/tutorial/first-steps/)
for framework basics.
