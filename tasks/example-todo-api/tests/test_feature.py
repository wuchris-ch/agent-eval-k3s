"""Hidden verification tests. Only exercise the public HTTP API so they are
robust to whatever internal refactoring the agent does. Order-independent:
never assume an empty store."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_todo(**fields) -> dict:
    payload = {"title": f"todo-{uuid.uuid4().hex[:8]}", **fields}
    resp = client.post("/todos", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_priority_defaults_to_2():
    todo = make_todo()
    assert todo["priority"] == 2


def test_priority_is_stored():
    todo = make_todo(priority=1)
    assert todo["priority"] == 1
    fetched = client.get(f"/todos/{todo['id']}").json()
    assert fetched["priority"] == 1


@pytest.mark.parametrize("bad", [0, 4, -1, 100])
def test_priority_out_of_range_rejected(bad):
    resp = client.post("/todos", json={"title": "x", "priority": bad})
    assert resp.status_code == 422


def test_filter_by_priority():
    p1 = make_todo(priority=1)
    p3 = make_todo(priority=3)
    resp = client.get("/todos", params={"priority": 1})
    assert resp.status_code == 200
    todos = resp.json()
    assert all(t["priority"] == 1 for t in todos)
    ids = {t["id"] for t in todos}
    assert p1["id"] in ids
    assert p3["id"] not in ids


def test_list_without_filter_returns_all_priorities():
    a = make_todo(priority=1)
    b = make_todo(priority=3)
    ids = {t["id"] for t in client.get("/todos").json()}
    assert {a["id"], b["id"]} <= ids


def test_delete_todo():
    todo = make_todo()
    resp = client.delete(f"/todos/{todo['id']}")
    assert resp.status_code == 204
    assert client.get(f"/todos/{todo['id']}").status_code == 404


def test_delete_missing_todo_404():
    assert client.delete("/todos/999999").status_code == 404


def test_existing_endpoints_still_work():
    todo = make_todo()
    assert client.get(f"/todos/{todo['id']}").json()["title"] == todo["title"]
    assert todo["done"] is False
