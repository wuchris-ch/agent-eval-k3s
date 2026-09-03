def label(values: dict[str, str], key: str) -> str:
    return values.get(key, "unknown")


def _self_test() -> None:
    assert label({"a": "Alpha"}, "a") == "Alpha"
    assert label({"a": "Alpha"}, "b") == "unknown"


if __name__ == "__main__":
    _self_test()
