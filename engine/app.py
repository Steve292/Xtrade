from fastapi import FastAPI
from .api import router as api_router

app = FastAPI(title="Extraction Engine API", version="0.1")
app.include_router(api_router, prefix="")
