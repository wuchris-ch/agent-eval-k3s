def _clean(value: str) -> str:
    return value.strip()


def display_name(first: str, last: str) -> str:
    return f"{_clean(first)} {_clean(last)}"
