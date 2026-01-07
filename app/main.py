from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse


app = FastAPI(
    title="Texet API",
    version="0.1.0",
    description="Base API scaffold for Texet.",
)


@app.get("/", response_class=JSONResponse)
def root() -> dict[str, str]:
    return {
        "message": "Texet API is running.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health", response_class=JSONResponse)
def health() -> dict[str, str]:
    return {"status": "ok"}
