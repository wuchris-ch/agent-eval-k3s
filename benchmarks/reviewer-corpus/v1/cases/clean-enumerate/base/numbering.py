def numbered(values: list[str]) -> list[str]:
    return [f"{index}: {values[index]}" for index in range(len(values))]


def _self_test() -> None:
    assert numbered(["a", "b"]) == ["0: a", "1: b"]


if __name__ == "__main__":
    _self_test()
