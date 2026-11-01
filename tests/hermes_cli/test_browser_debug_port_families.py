"""Port selection uses real IPv4 binds while unavailable IPv6 cannot force a busy fallback."""
import errno
from contextlib import ExitStack
import socket

import pytest

from hermes_cli import browser_connect


@pytest.mark.parametrize('failure_at', ['create', 'bind'])
def test_unavailable_ipv6_still_skips_an_occupied_ipv4_port(monkeypatch, failure_at):
    real_socket = socket.socket

    class IPv4OnlySocket(real_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family == socket.AF_INET6 and failure_at == 'create':
                raise OSError(errno.EAFNOSUPPORT, 'IPv6 unavailable')
            super().__init__(family, *args, **kwargs)

        def bind(self, address):
            if self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, 'IPv6 loopback unavailable')
            return super().bind(address)

    with real_socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen(1)
        blocked = occupied.getsockname()[1]
        monkeypatch.setattr(browser_connect.socket, 'socket', IPv4OnlySocket)
        selected = browser_connect.find_free_debug_port(blocked - 1)
        assert selected != blocked
        with real_socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', selected))


def test_available_ipv6_collision_is_still_checked():
    with ExitStack() as sockets:
        try:
            occupied = sockets.enter_context(socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
            occupied.bind(('::1', 0))
        except OSError:
            pytest.skip('This host has no bindable IPv6 loopback')
        occupied.listen(1)
        blocked = occupied.getsockname()[1]
        selected = browser_connect.find_free_debug_port(blocked - 1)
        assert selected != blocked
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ipv4:
            ipv4.bind(('127.0.0.1', selected))
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as ipv6:
                ipv6.bind(('::1', selected))
