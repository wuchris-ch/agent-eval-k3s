from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Todo API")


class TodoCreate(BaseModel):
    title: str
    priority: int = Field(default=2, ge=1, le=3)


class Todo(TodoCreate):
    id: int
    done: bool = False


_todos: dict[int, Todo] = {}
_next_id = 1


@app.post("/todos", response_model=Todo, status_code=201)
def create_todo(todo: TodoCreate) -> Todo:
    global _next_id
    item = Todo(id=_next_id, **todo.model_dump())
    _todos[item.id] = item
    _next_id += 1
    return item


@app.get("/todos", response_model=list[Todo])
def list_todos(priority: int | None = None) -> list[Todo]:
    todos = list(_todos.values())
    if priority is not None:
        todos = [t for t in todos if t.priority == priority]
    return todos


@app.get("/todos/{todo_id}", response_model=Todo)
def get_todo(todo_id: int) -> Todo:
    if todo_id not in _todos:
        raise HTTPException(status_code=404, detail="todo not found")
    return _todos[todo_id]


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int) -> Response:
    if todo_id not in _todos:
        raise HTTPException(status_code=404, detail="todo not found")
    del _todos[todo_id]
    return Response(status_code=204)
