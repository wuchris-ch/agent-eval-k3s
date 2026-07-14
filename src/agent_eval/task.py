"""Task definition: a task is a directory containing task.yaml, an environment
image, hidden tests, and an optional oracle solution overlay."""

from __future__ import annotations

import hashlib
import re
import stat
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .assurance import ChallengeSpec
from .outcome import AcceptancePolicy
DEFAULT_TASKS_ROOT = Path(__file__).resolve().parents[2] / "tasks"

DEFAULT_SANDBOX_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"},
    "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "4Gi"},
}

_QUANTITY_RE = re.compile(
    r"^(?P<number>(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<suffix>m|[kMGTPE]|[KMGTPE]i|[eE][+-]?\d+)?$"
)
_DECIMAL_SUFFIXES = {
    "m": Decimal("0.001"),
    "k": Decimal("1000"),
    "M": Decimal("1000000"),
    "G": Decimal("1000000000"),
    "T": Decimal("1000000000000"),
    "P": Decimal("1000000000000000"),
    "E": Decimal("1000000000000000000"),
}
_BINARY_SUFFIXES = {
    f"{prefix}i": Decimal(1024) ** exponent
    for exponent, prefix in enumerate("KMGTPE", start=1)
}


def _quantity_value(value: str) -> Decimal:
    match = _QUANTITY_RE.fullmatch(value)
    if match is None:
        raise ValueError("must be a positive Kubernetes resource quantity")
    number = Decimal(match.group("number"))
    suffix = match.group("suffix") or ""
    if suffix in _DECIMAL_SUFFIXES:
        number *= _DECIMAL_SUFFIXES[suffix]
    elif suffix in _BINARY_SUFFIXES:
        number *= _BINARY_SUFFIXES[suffix]
    elif suffix:
        number *= Decimal(10) ** int(suffix[1:])
    if number <= 0:
        raise ValueError("must be a positive Kubernetes resource quantity")
    return number


class ResourceQuantities(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    cpu: str
    memory: str
    ephemeral_storage: str = Field(alias="ephemeral-storage")

    @field_validator("cpu", "memory", "ephemeral_storage")
    @classmethod
    def _valid_quantity(cls, value: str) -> str:
        value = value.strip()
        _quantity_value(value)
        return value


class ResourceRequests(ResourceQuantities):
    cpu: str = DEFAULT_SANDBOX_RESOURCES["requests"]["cpu"]
    memory: str = DEFAULT_SANDBOX_RESOURCES["requests"]["memory"]
    ephemeral_storage: str = Field(
        default=DEFAULT_SANDBOX_RESOURCES["requests"]["ephemeral-storage"],
        alias="ephemeral-storage",
    )


class ResourceLimits(ResourceQuantities):
    cpu: str = DEFAULT_SANDBOX_RESOURCES["limits"]["cpu"]
    memory: str = DEFAULT_SANDBOX_RESOURCES["limits"]["memory"]
    ephemeral_storage: str = Field(
        default=DEFAULT_SANDBOX_RESOURCES["limits"]["ephemeral-storage"],
        alias="ephemeral-storage",
    )


class PodResources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: ResourceRequests = Field(default_factory=ResourceRequests)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @model_validator(mode="after")
    def _requests_within_limits(self) -> PodResources:
        for field in ("cpu", "memory", "ephemeral_storage"):
            request = _quantity_value(getattr(self.requests, field))
            limit = _quantity_value(getattr(self.limits, field))
            if request > limit:
                name = field.replace("_", "-")
                raise ValueError(f"{name} request must not exceed its limit")
        return self

    def as_kubernetes(self) -> dict[str, dict[str, str]]:
        return self.model_dump(by_alias=True)


class SandboxResources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: PodResources = Field(default_factory=PodResources)
    eval: PodResources = Field(default_factory=PodResources)


class Timeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_seconds: int = 900
    eval_seconds: int = 300

    @field_validator("agent_seconds", "eval_seconds")
    @classmethod
    def _positive_timeout(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("timeouts must be positive integers")
        return value


class JudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    weights: dict[str, float] = Field(
        default={"spec_adherence": 0.4, "maintainability": 0.4, "test_quality": 0.2}
    )

    @model_validator(mode="after")
    def _valid_weights(self) -> "JudgeConfig":
        if not self.weights or any(
            not name.strip() or weight <= 0 for name, weight in self.weights.items()
        ):
            raise ValueError("judge weights must have names and positive values")
        return self


class SandboxSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_as_non_root: bool = True
    run_as_user: int = Field(default=10001, ge=1)
    run_as_group: int = Field(default=10001, ge=1)
    read_only_root_filesystem: bool = True


class SandboxNetwork(BaseModel):
    """Egress policy for agent and evaluator pods.

    Evaluator egress is always denied.  Agent egress defaults to the bundled
    domain-aware proxy; ``open`` is an explicit compatibility escape hatch.
    """

    model_config = ConfigDict(extra="forbid")

    agent_mode: Literal["proxy", "open"] = "proxy"
    allowed_domains: list[str] = Field(default_factory=list)
    proxy_image: str = (
        "ubuntu/squid@sha256:"
        "6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"
    )

    @field_validator("allowed_domains")
    @classmethod
    def _valid_domains(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            domain = value.strip().lower()
            bare = domain.removeprefix(".")
            if (
                not bare
                or "://" in domain
                or "/" in domain
                or not re.fullmatch(r"[a-z0-9.-]+", bare)
            ):
                raise ValueError("allowed domains must be DNS suffixes, not URLs")
            normalized = f".{bare}"
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("proxy_image")
    @classmethod
    def _nonempty_image(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proxy_image must not be empty")
        return value.strip()


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    prompt: str
    language: str = "python"
    tags: list[str] = Field(default_factory=list)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    resources: SandboxResources = Field(default_factory=SandboxResources)
    security: SandboxSecurity = Field(default_factory=SandboxSecurity)
    network: SandboxNetwork = Field(default_factory=SandboxNetwork)
    # Runs inside the eval pod with cwd=/workspace; hidden tests are mounted at
    # /tests and machine-readable output must be written under /results.
    test_command: str
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    acceptance: AcceptancePolicy = Field(default_factory=AcceptancePolicy)
    challenges: list[ChallengeSpec] = Field(default_factory=list)
    path: Path

    @field_validator("prompt")
    @classmethod
    def _prompt_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task prompt must not be empty")
        return v

    @property
    def image_tag(self) -> str:
        return f"agent-eval/{self.id}:{self.environment_sha256[:12]}"

    @property
    def environment_sha256(self) -> str:
        """Hash the complete local Docker build context, including file modes."""

        digest = hashlib.sha256()
        if not self.environment_dir.is_dir():
            return digest.hexdigest()
        for candidate in sorted(self.environment_dir.rglob("*")):
            relative = candidate.relative_to(self.environment_dir)
            name = relative.as_posix()
            metadata = candidate.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(
                    f"L\0{name}\0{mode:o}\0{candidate.readlink()}\n".encode()
                )
            elif stat.S_ISDIR(metadata.st_mode):
                digest.update(f"D\0{name}\0{mode:o}\n".encode())
            elif stat.S_ISREG(metadata.st_mode):
                digest.update(f"F\0{name}\0{mode:o}\0{metadata.st_size}\0".encode())
                digest.update(candidate.read_bytes())
                digest.update(b"\n")
            else:
                digest.update(f"S\0{name}\0{mode:o}\0{metadata.st_mode}\n".encode())
        return digest.hexdigest()

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
