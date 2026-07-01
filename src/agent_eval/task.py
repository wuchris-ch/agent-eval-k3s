"""Task definition: a task is a directory containing task.yaml, an environment
image, hidden tests, and an optional oracle solution overlay."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_TASKS_ROOT = Path(__file__).resolve().parents[2] / "tasks"


class Timeouts(BaseModel):
    agent_seconds: int = 900
    eval_seconds: int = 300


class JudgeConfig(BaseModel):
    enabled: bool = True
    weights: dict[str, float] = Field(
        default={"spec_adherence": 0.4, "maintainability": 0.4, "test_quality": 0.2}
    )


class Task(BaseModel):
    id: str
    prompt: str
    language: str = "python"
    tags: list[str] = Field(default_factory=list)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    # Runs inside the eval pod with cwd=/workspace; hidden tests are mounted at
    # /tests and machine-readable output must be written under /results.
    test_command: str
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    path: Path

    @field_validator("prompt")
    @classmethod
    def _prompt_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task prompt must not be empty")
        return v

    @property
    def image_tag(self) -> str:
        return f"agent-eval/{self.id}:latest"

    @property
    def environment_dir(self) -> Path:
        return self.path / "environment"

    @property
    def workspace_dir(self) -> Path:
        return self.environment_dir / "workspace"

    @property
    def tests_dir(self) -> Path:
        return self.path / "tests"

    @property
    def solution_dir(self) -> Path:
        return self.path / "solution"

    def validate_layout(self) -> list[str]:
        problems = []
        if not (self.environment_dir / "Dockerfile").is_file():
            problems.append("missing environment/Dockerfile")
        if not self.workspace_dir.is_dir():
            problems.append("missing environment/workspace/ starter directory")
        if not self.tests_dir.is_dir() or not any(self.tests_dir.iterdir()):
            problems.append("missing or empty tests/ directory")
        return problems


def load_task(task_id: str, tasks_root: Path = DEFAULT_TASKS_ROOT) -> Task:
    task_dir = tasks_root / task_id
    yaml_path = task_dir / "task.yaml"
    if not yaml_path.is_file():
        raise FileNotFoundError(f"no task.yaml at {yaml_path}")
    data = yaml.safe_load(yaml_path.read_text())
    data["path"] = task_dir
    task = Task.model_validate(data)
    if task.id != task_id:
        raise ValueError(f"task.yaml id {task.id!r} does not match directory {task_id!r}")
    return task


def list_tasks(tasks_root: Path = DEFAULT_TASKS_ROOT) -> list[Task]:
    tasks = []
    if not tasks_root.is_dir():
        return tasks
    for entry in sorted(tasks_root.iterdir()):
        if (entry / "task.yaml").is_file():
            tasks.append(load_task(entry.name, tasks_root))
    return tasks
