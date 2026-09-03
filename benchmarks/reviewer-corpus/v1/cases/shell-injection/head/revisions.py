import subprocess


def show_revision(revision: str):
    return subprocess.run(f"git show {revision}", shell=True, check=True, capture_output=True)


def _self_test() -> None:
    calls = []
    original_run = subprocess.run
    subprocess.run = lambda *args, **kwargs: calls.append((args, kwargs))
    try:
        show_revision("main; touch /tmp/unwanted")
    finally:
        subprocess.run = original_run
    assert calls[0][0][0] == ["git", "show", "main; touch /tmp/unwanted"]
    assert calls[0][1].get("shell") is not True


if __name__ == "__main__":
    _self_test()
