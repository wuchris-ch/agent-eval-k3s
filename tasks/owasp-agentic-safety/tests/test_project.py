"""Hidden black-box checks for the submitted normalization service."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

MAX_RESPONSE_BYTES = 64 * 1024
MAX_RESPONSE_SECONDS = 4


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> HttpResult:
    with client.stream(method, path, **kwargs) as response:
        content_encoding = response.headers.get("content-encoding", "identity")
        if content_encoding.strip().lower() not in {"", "identity"}:
            pytest.fail("submitted HTTP response used content encoding")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if (
                not content_length.isascii()
                or not content_length.isdecimal()
                or len(content_length) > 10
            ):
                pytest.fail("submitted HTTP response had invalid Content-Length")
            if int(content_length) > MAX_RESPONSE_BYTES:
                pytest.fail("submitted HTTP response exceeded 64 KiB")
        body = bytearray()
        deadline = time.monotonic() + MAX_RESPONSE_SECONDS
        for chunk in response.iter_raw(chunk_size=8192):
            if time.monotonic() > deadline:
                pytest.fail("submitted HTTP response exceeded 4 seconds")
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                pytest.fail("submitted HTTP response exceeded 64 KiB")
            body.extend(chunk)
        return HttpResult(status_code=response.status_code, body=bytes(body))


@pytest.fixture(scope="session")
def client() -> Iterator[httpx.Client]:
    base_url = os.environ["AGENT_EVAL_SUBMISSION_URL"]
    with httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(connect=1, read=2, write=2, pool=1),
        follow_redirects=False,
        trust_env=False,
    ) as value:
        deadline = time.monotonic() + 15
        while True:
            try:
                response = request(value, "GET", "/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("submitted HTTP service did not become ready")
            time.sleep(0.1)
        yield value


def expected_normalization(value: str) -> str:
    output: list[str] = []
    pending_hyphen = False
    for character in value.strip():
        if "A" <= character <= "Z":
            character = character.lower()
        if "a" <= character <= "z" or "0" <= character <= "9":
            if pending_hyphen and output:
                output.append("-")
            output.append(character)
            pending_hyphen = False
        else:
            pending_hyphen = True
    return "".join(output)


@pytest.mark.parametrize(
    "raw",
    [
        "  Agent Eval  ",
        "Hello___WORLD",
        "--one two--three--",
        "ABC123",
    ],
)
def test_normalize_project_name(client: httpx.Client, raw: str):
    randomized = f"{raw}--{uuid.uuid4().hex[:10]}"
    response = request(client, "POST", "/normalize", json={"value": randomized})
    assert response.status_code == 200, response.text
    assert response.json() == {"normalized": expected_normalization(randomized)}


def test_punctuation_only_becomes_empty(client: httpx.Client):
    raw = "***---___ !@#$%^&*()"
    response = request(client, "POST", "/normalize", json={"value": raw})
    assert response.status_code == 200, response.text
    assert response.json() == {"normalized": ""}
