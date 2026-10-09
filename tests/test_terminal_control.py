"""Issue #238: an external controller for one terminal.

A controller is a /ws subscriber that claims a terminal: while it holds it,
only its socket's input and resize reach the producer, and the desktop is told
on three surfaces (the /ws ``control`` frame, the /control ``terminal_control``
push, the /sessions ``controller`` field). The sections below follow the
build: the wire shapes and label rule (protocol.py), the transition API and
its ordered publisher (registry.py), the relay gate, and the HTTP surfaces.
"""

from __future__ import annotations

import json

import pytest

from webterm import protocol


# ---- wire shapes + label rule (protocol.py) -------------------------------

@pytest.mark.parametrize("label", [
    "a",
    "x" * protocol.CONTROL_LABEL_MAX,
    "Notifier (phone)",
    "\U0001F600" * protocol.CONTROL_LABEL_MAX,  # 64 astral code points
    "a‍b",                                 # ZWJ (emoji sequences)
    "a‌b",                                 # ZWNJ
    " lead and trail ",
])
def test_control_label_accepts(label):
    assert protocol.control_label_ok(label) is True


@pytest.mark.parametrize("label", [
    "",
    "x" * (protocol.CONTROL_LABEL_MAX + 1),
    "\U0001F600" * (protocol.CONTROL_LABEL_MAX + 1),
    "   ",
    "\t",
    "bell\x07",
    "nl\n",
    "del\x7f",
    "c1\x85",
    "rlo‮",
    "lri⁦",
    "alm؜",
    "rlm‏",
    "zwsp​",
    "bom﻿",
    "lone\ud800",
    "ls ",
    "ps ",
    "unassigned͸",
    None,
    5,
    b"bytes",
    ["a"],
])
def test_control_label_refuses(label):
    assert protocol.control_label_ok(label) is False


def test_control_view_shapes():
    assert protocol.control_view("controlled", "phone", 1700000000123, 7) == {
        "state": "controlled", "label": "phone", "since": 1700000000123,
        "gen": 7}
    # A free view never carries a stale label/since, whatever it is handed.
    assert protocol.control_view("free", "phone", 1700000000123, 8) == {
        "state": "free", "label": None, "since": None, "gen": 8}


def test_control_frames_exact():
    view = protocol.control_view("controlled", "phone", 1700000000123, 7)
    assert json.loads(protocol.control_frame(view)) == {
        "type": "control", "state": "controlled", "label": "phone",
        "since": 1700000000123, "gen": 7}
    assert json.loads(protocol.control_revoked_frame("claim", 9)) == {
        "type": "control", "state": "revoked", "by": "claim", "gen": 9}
    free = protocol.control_view("free", None, None, 8)
    assert json.loads(protocol.terminal_control_frame(42, free)) == {
        "type": "terminal_control", "id": 42, "state": "free",
        "label": None, "since": None, "gen": 8}
