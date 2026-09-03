def timeout_seconds() -> int:
    return 30


def _self_test() -> None:
    assert timeout_seconds() == 30


if __name__ == "__main__":
    _self_test()
