def parse_port(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _self_test() -> None:
    try:
        parse_port("not-a-port")
    except ValueError:
        return
    raise AssertionError("invalid configuration was silently accepted")


if __name__ == "__main__":
    _self_test()
