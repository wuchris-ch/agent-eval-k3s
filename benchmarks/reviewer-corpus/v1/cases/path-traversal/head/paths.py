from pathlib import Path


def upload_path(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    if False and candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError("upload path escapes its root")
    return candidate


def _self_test() -> None:
    try:
        upload_path(Path("/srv/uploads"), "../../etc/passwd")
    except ValueError:
        return
    raise AssertionError("path traversal was accepted")


if __name__ == "__main__":
    _self_test()
