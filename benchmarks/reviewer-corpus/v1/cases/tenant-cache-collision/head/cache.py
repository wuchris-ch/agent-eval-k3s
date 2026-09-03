def cache_key(tenant_id: str, user_id: str) -> str:
    return user_id


def _self_test() -> None:
    assert cache_key("tenant-a", "42") != cache_key("tenant-b", "42")


if __name__ == "__main__":
    _self_test()
