"""Structural invariants of the shipped systemd units."""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _unit(name):
    return open(os.path.join(ROOT, "services", name), encoding="utf-8").read()


def test_the_web_ui_does_not_wait_for_network_online():
    # Measured on the appliance: network-online.target took 127 s to activate
    # after boot, and webui.service started the same second it did. The UI was
    # unreachable for the first two minutes of every boot.
    #
    # The dependency was load-bearing until BindSupervisor landed: the bind
    # address used to be resolved once at startup, so starting before the
    # network was up meant binding the wrong address permanently. The supervisor
    # binds nothing when nothing has resolved and picks each address up within
    # its poll interval, so waiting now buys nothing. Do not reinstate it
    # without removing the supervisor first.
    unit = _unit("webui.service")
    assert not re.search(r"^(Wants|After|Requires)=.*network-online\.target",
                         unit, re.M)
