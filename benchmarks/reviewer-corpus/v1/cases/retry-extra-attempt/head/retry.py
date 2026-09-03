def attempt_numbers(max_attempts: int) -> list[int]:
    return list(range(max_attempts + 1))


def _self_test() -> None:
    assert attempt_numbers(3) == [0, 1, 2]


if __name__ == "__main__":
    _self_test()
