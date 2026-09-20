import hmac

from fastapi import HTTPException, Request


def authorize(request: Request, actor: str) -> None:
    expected = getattr(
        request.app.state.settings, actor + "_api_key"
    ).get_secret_value()
    if expected and not hmac.compare_digest(
        request.headers.get("authorization", "").encode(),
        ("Bearer " + expected).encode(),
    ):
        raise HTTPException(
            401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"}
        )


def phone_access(request: Request) -> None:
    authorize(request, "phone")


def server_access(request: Request) -> None:
    authorize(request, "server")
