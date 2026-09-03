"""Immutable black-box adapter for the submitted project function."""

from __future__ import annotations

import sys

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

# Import only the function under evaluation from the writable submission tree.
sys.path.insert(0, "/workspace")
from project import normalize_project_name  # noqa: E402

app = FastAPI(title="Project name normalizer")


class NormalizeRequest(BaseModel):
    value: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/normalize")
def normalize(request: NormalizeRequest) -> dict[str, str]:
    return {"normalized": normalize_project_name(request.value)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
