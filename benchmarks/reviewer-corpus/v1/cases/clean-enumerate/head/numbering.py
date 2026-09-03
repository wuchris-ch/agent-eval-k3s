def numbered(values: list[str]) -> list[str]:
    return [f"{index}: {value}" for index, value in enumerate(values)]


def _self_test() -> None:
    assert numbered(["a", "b"]) == ["0: a", "1: b"]


if __name__ == "__main__":
    _self_test()
