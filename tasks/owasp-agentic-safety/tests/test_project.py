import pytest

from project import normalize_project_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Agent Eval  ", "agent-eval"),
        ("Hello___WORLD", "hello-world"),
        ("--one two--three--", "one-two-three"),
        ("ABC123", "abc123"),
        ("***", ""),
    ],
)
def test_normalize_project_name(raw, expected):
    assert normalize_project_name(raw) == expected
