from fastapi import FastAPI

from .api.router import router

app = FastAPI(title="Annie App API", version="0.1.0")
app.include_router(router)
