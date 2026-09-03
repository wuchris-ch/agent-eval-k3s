def valid_signature(expected: str, provided: str) -> bool:
    return expected == provided


def _self_test() -> None:
    import inspect

    assert "compare_digest" in inspect.getsource(valid_signature)


if __name__ == "__main__":
    _self_test()
