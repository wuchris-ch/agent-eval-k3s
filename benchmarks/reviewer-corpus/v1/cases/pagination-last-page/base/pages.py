def requested_pages(page_count: int) -> list[int]:
    return list(range(1, page_count + 1))


def _self_test() -> None:
    assert requested_pages(3) == [1, 2, 3]


if __name__ == "__main__":
    _self_test()
