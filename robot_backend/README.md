# Annie robot service scaffold

Owner: Roger. This top-level folder is reserved for the team's robot-side service.
It currently exposes `GET /health`; simulator policy, inference, and the demo
UI are isolated under [`robot/robot_backend/`](../robot/robot_backend/README.md).

From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8001
```

The simulator demo also uses port 8000. Run these on separate ports/hosts when
using both. See the [robot workspace](../robot/README.md) for the isolated
simulator, its API contract, frontend, and launch commands.
