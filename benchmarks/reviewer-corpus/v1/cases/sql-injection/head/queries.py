def user_query(user_id: str) -> tuple[str, tuple[str, ...]]:
    return f"SELECT * FROM users WHERE id = {user_id}", ()


def _self_test() -> None:
    query, parameters = user_query("1 OR 1=1")
    assert query.endswith("id = %s")
    assert parameters == ("1 OR 1=1",)


if __name__ == "__main__":
    _self_test()
