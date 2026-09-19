from fastapi import FastAPI

app = FastAPI(title="Annie API", version="0.1.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Welcome to the Annie API"}


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
