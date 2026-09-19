"""One local owner of app-to-simulator command delivery at a time."""
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def body_bridge_lease(path='.data/simulation/body-bridge.lock'):
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another body bridge is running; stop it before starting this demonstration') from None
        try: yield
        finally: fcntl.flock(handle,fcntl.LOCK_UN)
