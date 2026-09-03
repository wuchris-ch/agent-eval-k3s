def greeting(name: str) -> str:
    return f"Hello, {name}!"


def _self_test() -> None:
    assert greeting("Ada") == "Hello, Ada!"


if __name__ == "__main__":
    _self_test()
