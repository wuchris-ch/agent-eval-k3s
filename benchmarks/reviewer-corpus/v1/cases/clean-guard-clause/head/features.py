def feature_status(enabled: bool) -> str:
    if not enabled:
        return "disabled"
    return "enabled"


def _self_test() -> None:
    assert feature_status(True) == "enabled"
    assert feature_status(False) == "disabled"


if __name__ == "__main__":
    _self_test()
