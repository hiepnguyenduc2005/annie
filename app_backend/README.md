# Annie app service scaffold

Owner: Ellis. This top-level folder is reserved for the team's application API.
It currently exposes `GET /health`; simulator policy, inference, and the demo
UI are isolated under [`robot/app_backend/`](../robot/app_backend/README.md).

From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

The simulator demo also uses port 8000. Run these on separate ports/hosts when
using both. See the [robot workspace](../robot/README.md) for the isolated
simulator, its API contract, frontend, and launch commands.
