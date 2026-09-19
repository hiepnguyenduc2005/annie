# Robot backend

This folder is an independent FastAPI service. Copy this folder to its server
and install its own dependencies. Python 3.10 or newer is recommended.

From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8001
```

Health: http://127.0.0.1:8001/health
API docs: http://127.0.0.1:8001/docs

For a server process, omit `--reload`; binding and network access depend on the
server deployment. Both servers can use port 8000 when on different hosts.
The differing development ports let both run on the same computer.

See the [project architecture](../README.md) for responsibilities and privacy rules.
Only health endpoints are wired up; feature packages are placeholders.