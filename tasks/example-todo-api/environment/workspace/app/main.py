from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Todo API")


class TodoCreate(BaseModel):
    title: str


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
def list_todos() -> list[Todo]:
    return list(_todos.values())


@app.get("/todos/{todo_id}", response_model=Todo)
def get_todo(todo_id: int) -> Todo:
    if todo_id not in _todos:
        raise HTTPException(status_code=404, detail="todo not found")
    return _todos[todo_id]
