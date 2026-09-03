def request_options() -> dict[str, object]:
    return {"timeout": 10, "verify": True}


def _self_test() -> None:
    assert request_options()["verify"] is True


if __name__ == "__main__":
    _self_test()
