def valid_percentage(value: int) -> bool:
    return 0 < value < 100


def _self_test() -> None:
    assert valid_percentage(0)
    assert valid_percentage(100)


if __name__ == "__main__":
    _self_test()
