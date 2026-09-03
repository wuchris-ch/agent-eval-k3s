def audit_token(token: str) -> str:
    return token


def _self_test() -> None:
    token = "secret-value-123"
    assert token not in audit_token(token)


if __name__ == "__main__":
    _self_test()
