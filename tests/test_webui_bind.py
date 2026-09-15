"""The web UI's bind supervisor.

``webui.bind_interfaces`` is a multi-select, and this device's interfaces come
and go while it runs: the iPhone hotspot appears when a sync starts, WireGuard
comes up after it, WiFi drops and returns. Binding once at startup to a single
address strands the UI on whichever interface happened to be up at that moment,
with nothing systemd can see. The supervisor keeps one listener per resolved
address and reconciles the set on a timer.
"""
import os
import tempfile
import threading

import pytest

SANDBOX = tempfile.mkdtemp(prefix="ibm_bind_")
os.environ.setdefault("IOSBACKUP_RUNTIME_DIR", os.path.join(SANDBOX, "rt"))
os.environ.setdefault("IOSBACKUP_LOG_DIR", os.path.join(SANDBOX, "log"))
os.environ.setdefault("IOSBACKUP_CONFIG", os.path.join(SANDBOX, "config.yaml"))

try:
    import webui
except Exception as exc:  # pragma: no cover - environment without flask
    pytest.skip(f"webui not importable: {exc}", allow_module_level=True)


class FakeServer:
    """Stands in for a werkzeug server: blocks in serve_forever until shutdown."""

    def __init__(self, address, port):
        self.address = address
        self.port = port
        self.started = threading.Event()
        self._stop = threading.Event()
        self.shutdown_calls = 0

    def serve_forever(self):
        self.started.set()
        self._stop.wait(5)

    def shutdown(self):
        self.shutdown_calls += 1
        self._stop.set()


class Factory:
    """Records every server it is asked to build; can be told to refuse one."""

    def __init__(self, refuse=()):
        self.built = []
        self.refuse = set(refuse)

    def __call__(self, address, port):
        if address in self.refuse:
            raise OSError(f"cannot assign requested address {address}")
        server = FakeServer(address, port)
        self.built.append(server)
        return server

    def addresses_built(self):
        return [s.address for s in self.built]


def _supervisor(addresses, factory=None):
    """A supervisor whose resolver returns whatever `addresses` holds now."""
    factory = factory or Factory()
    sup = webui.BindSupervisor(
        app=webui.app,
        port=8080,
        bind_interfaces=["usb_iphone", "wireguard"],
        resolver=lambda: list(addresses),
        server_factory=factory,
    )
    return sup, factory


def test_binds_one_listener_per_resolved_address():
    addresses = ["172.20.10.2", "10.7.0.3"]
    sup, factory = _supervisor(addresses)
    try:
        sup.reconcile()
        assert sorted(sup.bound_addresses()) == ["10.7.0.3", "172.20.10.2"]
        assert sorted(factory.addresses_built()) == ["10.7.0.3", "172.20.10.2"]
    finally:
        sup.shutdown_all()


def test_binds_an_address_that_appears_later():
    # WireGuard coming up mid-sync. Fails if the address set is resolved once.
    addresses = ["172.20.10.2"]
    sup, factory = _supervisor(addresses)
    try:
        sup.reconcile()
        addresses.append("10.7.0.3")
        sup.reconcile()
        assert sorted(sup.bound_addresses()) == ["10.7.0.3", "172.20.10.2"]
    finally:
        sup.shutdown_all()


def test_drops_an_address_that_disappears():
    addresses = ["172.20.10.2", "10.7.0.3"]
    sup, factory = _supervisor(addresses)
    try:
        sup.reconcile()
        gone = next(s for s in factory.built if s.address == "10.7.0.3")
        addresses.remove("10.7.0.3")
        sup.reconcile()
        assert sup.bound_addresses() == ["172.20.10.2"]
        assert gone.shutdown_calls == 1
    finally:
        sup.shutdown_all()


def test_leaves_a_surviving_listener_untouched():
    # Fails if reconcile tears down and rebuilds the whole set each pass, which
    # would drop every open connection every interval.
    addresses = ["172.20.10.2"]
    sup, factory = _supervisor(addresses)
    try:
        sup.reconcile()
        first = factory.built[0]
        addresses.append("10.7.0.3")
        sup.reconcile()
        assert factory.addresses_built().count("172.20.10.2") == 1
        assert first.shutdown_calls == 0
    finally:
        sup.shutdown_all()


def test_binds_nothing_when_no_selected_interface_has_an_address():
    # No silent widening to 0.0.0.0: the operator restricted where the UI answers.
    addresses = ["172.20.10.2"]
    sup, factory = _supervisor(addresses)
    try:
        sup.reconcile()
        del addresses[:]
        sup.reconcile()
        assert sup.bound_addresses() == []
    finally:
        sup.shutdown_all()


def test_one_address_that_refuses_to_bind_does_not_block_the_others():
    # A stale WireGuard IP still in the interface list is EADDRNOTAVAIL. The
    # hotspot must still be served, and the failed address retried next pass.
    addresses = ["172.20.10.2", "10.7.0.3"]
    factory = Factory(refuse={"10.7.0.3"})
    sup, factory = _supervisor(addresses, factory)
    try:
        sup.reconcile()
        assert sup.bound_addresses() == ["172.20.10.2"]
        factory.refuse.clear()
        sup.reconcile()
        assert sorted(sup.bound_addresses()) == ["10.7.0.3", "172.20.10.2"]
    finally:
        sup.shutdown_all()
