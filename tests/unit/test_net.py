from stellasaurus.control import net


def test_is_tailnet_range():
    assert net._is_tailnet("100.70.106.107") is True  # CGNAT / Tailscale
    assert net._is_tailnet("100.64.0.0") is True
    assert net._is_tailnet("10.0.1.101") is False  # LAN
    assert net._is_tailnet("127.0.0.1") is False
    assert net._is_tailnet("not-an-ip") is False


def test_resolve_localhost_mode():
    assert net.resolve_bind_hosts("localhost", None) == ["127.0.0.1"]


def test_resolve_all_mode():
    assert net.resolve_bind_hosts("all", None) == ["0.0.0.0"]


def test_host_override_wins():
    assert net.resolve_bind_hosts("tailnet", "192.168.1.5") == ["192.168.1.5"]


def test_tailnet_mode_includes_loopback(monkeypatch):
    monkeypatch.setattr(net, "tailscale_ipv4", lambda: "100.70.106.107")
    assert net.resolve_bind_hosts("tailnet", None) == ["127.0.0.1", "100.70.106.107"]


def test_tailnet_mode_falls_back_to_loopback_when_no_tailscale(monkeypatch):
    monkeypatch.setattr(net, "tailscale_ipv4", lambda: None)
    assert net.resolve_bind_hosts("tailnet", None) == ["127.0.0.1"]
