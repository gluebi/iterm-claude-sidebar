"""Who may reach the daemon: bind address, allowed peers and allowed Host/Origin names.

Defaults keep it loopback-only. Opening it to a reverse proxy on the LAN requires listing that proxy's address.
"""
import ipaddress
from urllib.parse import urlsplit

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def load(config: dict, port: int):
    """-> (bind address, allowed networks, allowed Host values). Exits when it would be open without an allow-list."""
    bind = config.get("bind", "127.0.0.1")
    extra = [ipaddress.ip_network(n, strict=False) for n in config.get("allow_from", [])]
    if not ipaddress.ip_address(bind).is_loopback and not extra:
        raise SystemExit(f"config: bind={bind} is reachable from the network; set allow_from to the reverse proxy's address (e.g. [\"192.168.1.10\"])")
    hosts = {f"127.0.0.1:{port}", *config.get("allowed_hosts", [])}
    if bind not in ("0.0.0.0", "::"):
        hosts.add(f"{bind}:{port}")  # the page is then also reachable under the bind address itself
    return bind, LOOPBACK + extra, hosts


def peer_allowed(peer: str | None, networks) -> bool:
    try:
        return any(ipaddress.ip_address(peer) in net for net in networks)
    except ValueError:
        return False


def origin_allowed(origin: str, hosts) -> bool:
    return urlsplit(origin).netloc in hosts


if __name__ == "__main__":
    bind, nets, hosts = load({}, 47831)
    assert bind == "127.0.0.1" and hosts == {"127.0.0.1:47831"}
    assert peer_allowed("127.0.0.1", nets) and peer_allowed("::1", nets) and not peer_allowed("192.168.1.10", nets)
    bind, nets, hosts = load({"bind": "0.0.0.0", "allow_from": ["192.168.1.10", "10.0.0.0/24"], "allowed_hosts": ["sidebar.example.org"]}, 47831)
    assert peer_allowed("192.168.1.10", nets) and peer_allowed("10.0.0.7", nets) and not peer_allowed("192.168.1.11", nets) and not peer_allowed("10.0.1.7", nets)
    assert load({"bind": "192.168.1.5", "allow_from": ["192.168.1.10"]}, 47831)[2] >= {"192.168.1.5:47831", "127.0.0.1:47831"}
    assert not peer_allowed(None, nets) and not peer_allowed("not-an-ip", nets)
    assert origin_allowed("https://sidebar.example.org", hosts) and not origin_allowed("https://evil.example", hosts) and not origin_allowed("null", hosts)
    try:
        load({"bind": "0.0.0.0"}, 47831)
        raise AssertionError("open bind without allow_from must be refused")
    except SystemExit:
        pass
    print("ok")
