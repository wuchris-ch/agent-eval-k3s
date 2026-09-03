def squares(values: list[int]) -> list[int]:
    result = []
    for value in values:
        result.append(value * value)
    return result


def _self_test() -> None:
    assert squares([1, 2, 3]) == [1, 4, 9]


if __name__ == "__main__":
    _self_test()
