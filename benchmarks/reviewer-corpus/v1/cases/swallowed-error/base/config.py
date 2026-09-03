def parse_port(value: str) -> int:
    return int(value)


def _self_test() -> None:
    try:
        parse_port("not-a-port")
    except ValueError:
        return
    raise AssertionError("invalid configuration was silently accepted")


if __name__ == "__main__":
    _self_test()
