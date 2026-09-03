DEFAULT_TIMEOUT_SECONDS = 30


def timeout_seconds() -> int:
    return DEFAULT_TIMEOUT_SECONDS


def _self_test() -> None:
    assert timeout_seconds() == 30


if __name__ == "__main__":
    _self_test()
