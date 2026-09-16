import pytest

from execraft.network import is_loopback_host


@pytest.mark.parametrize(
    "value",
    ["localhost", "LOCALHOST.", "127.0.0.1", "127.42.1.9", "::1", "  ::1  "],
)
def test_is_loopback_host_accepts_loopback_forms(value: str) -> None:
    assert is_loopback_host(value)


@pytest.mark.parametrize("value", ["", "example.com", "192.168.1.10", "::2", "localhost.local"])
def test_is_loopback_host_rejects_non_loopback_hosts(value: str) -> None:
    assert not is_loopback_host(value)
