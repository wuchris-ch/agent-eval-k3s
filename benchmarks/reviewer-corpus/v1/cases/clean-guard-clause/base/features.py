def feature_status(enabled: bool) -> str:
    if enabled:
        return "enabled"
    else:
        return "disabled"


def _self_test() -> None:
    assert feature_status(True) == "enabled"
    assert feature_status(False) == "disabled"


if __name__ == "__main__":
    _self_test()
