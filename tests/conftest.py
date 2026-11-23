"""Fixtures shared by the Color Kinetics tests."""

from __future__ import annotations

import socket

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def block_real_kinet_sockets(monkeypatch):
    """Fail any test that would transmit real KiNET (UDP 6038) traffic."""
    real_socket = socket.socket

    def guarded(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        original = sock.sendto

        def sendto(data, *send_args, **send_kwargs):
            addr = send_kwargs.get("address")
            if send_args and isinstance(send_args[-1], tuple):
                addr = send_args[-1]
            if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] == 6038:
                raise AssertionError(f"refusing real KiNET send to {addr}")
            return original(data, *send_args, **send_kwargs)

        sock.sendto = sendto
        return sock

    monkeypatch.setattr(socket, "socket", guarded)


@pytest.fixture
def kinet_script():
    """A supply that answers like the captured Data Enabler Pro."""
    from .fake_kinet import SupplyScript

    return SupplyScript()


@pytest.fixture
def fake_udp(kinet_script, monkeypatch):
    """Route every KiNET socket in the test through ``kinet_script``."""
    from .fake_kinet import install_fake_endpoint

    install_fake_endpoint(monkeypatch, kinet_script)
    return kinet_script
