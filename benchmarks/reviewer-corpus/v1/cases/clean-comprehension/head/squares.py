def squares(values: list[int]) -> list[int]:
    return [value * value for value in values]


def _self_test() -> None:
    assert squares([1, 2, 3]) == [1, 4, 9]


if __name__ == "__main__":
    _self_test()
