def transfer(balances: dict[str, int], source: str, destination: str, amount: int) -> None:
    balances[source] -= amount
    balances[destination] += amount


def _self_test() -> None:
    balances = {"source": 100, "destination": 20}
    transfer(balances, "source", "destination", 10)
    assert balances == {"source": 90, "destination": 30}
    assert sum(balances.values()) == 120


if __name__ == "__main__":
    _self_test()
