"""Hidden black-box checks for the submitted HTTP service."""

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
                response = request(value, "GET", "/openapi.json")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("submitted HTTP service did not become ready")
            time.sleep(0.1)
        yield value


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


def make_todo(client: httpx.Client, **fields: Any) -> dict[str, Any]:
    payload = {"title": f"todo-{uuid.uuid4().hex[:8]}", **fields}
    response = request(client, "POST", "/todos", json=payload)
    assert response.status_code == 201, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def test_priority_defaults_to_2(client: httpx.Client):
    todo = make_todo(client)
    assert todo["priority"] == 2


def test_priority_is_stored(client: httpx.Client):
    todo = make_todo(client, priority=1)
    assert todo["priority"] == 1
    fetched = request(client, "GET", f"/todos/{todo['id']}").json()
    assert fetched["priority"] == 1


@pytest.mark.parametrize("bad", [0, 4, -1, 100])
def test_priority_out_of_range_rejected(client: httpx.Client, bad: int):
    response = request(
        client,
        "POST",
        "/todos",
        json={"title": f"invalid-{uuid.uuid4().hex}", "priority": bad},
    )
    assert response.status_code == 422


def test_filter_by_priority(client: httpx.Client):
    priority_one = make_todo(client, priority=1)
    priority_three = make_todo(client, priority=3)
    response = request(client, "GET", "/todos", params={"priority": 1})
    assert response.status_code == 200
    todos = response.json()
    assert all(todo["priority"] == 1 for todo in todos)
    identifiers = {todo["id"] for todo in todos}
    assert priority_one["id"] in identifiers
    assert priority_three["id"] not in identifiers


def test_list_without_filter_returns_all_priorities(client: httpx.Client):
    first = make_todo(client, priority=1)
    second = make_todo(client, priority=3)
    response = request(client, "GET", "/todos")
    assert response.status_code == 200
    identifiers = {todo["id"] for todo in response.json()}
    assert {first["id"], second["id"]} <= identifiers


def test_delete_todo(client: httpx.Client):
    todo = make_todo(client)
    response = request(client, "DELETE", f"/todos/{todo['id']}")
    assert response.status_code == 204
    assert request(client, "GET", f"/todos/{todo['id']}").status_code == 404


def test_delete_missing_todo_404(client: httpx.Client):
    missing = 900_000_000 + int(uuid.uuid4().hex[:6], 16)
    assert request(client, "DELETE", f"/todos/{missing}").status_code == 404


def test_existing_endpoints_still_work(client: httpx.Client):
    todo = make_todo(client)
    fetched = request(client, "GET", f"/todos/{todo['id']}").json()
    assert fetched["title"] == todo["title"]
    assert todo["done"] is False
