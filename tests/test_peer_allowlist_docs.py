"""#252: the peer proxy's documented surface IS the code's (R1).

The allowlist is an explicit table in ``webterm/broker/peers.py`` and an
explicit table in ``wiki/Technical-Reference.md`` ("Peer proxy (#252)"), with
methods. This parses the wiki's tables and holds them equal to the code's,
and pins the other numbers and names the section states: the two header
names, the sandbox policy, the refusal codes, the dial caps and the timeouts.
"""

from __future__ import annotations

import re
from pathlib import Path

from webterm.broker import peers

REPO = Path(__file__).resolve().parents[1]


def _section():
    text = (REPO / "wiki" / "Technical-Reference.md").read_text(
        encoding="utf-8")
    a = text.index("### Peer proxy (#252)")
    b = text.index("\n## ", a)
    return text[a:b]


def _table(section, heading):
    a = section.index(heading)
    b = section.find("\n#### ", a + 1)
    block = section[a:b if b != -1 else len(section)]
    pairs = set()
    for line in block.splitlines():
        if not line.startswith("| ") or line.startswith("| Method "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        methods = [m.strip() for m in cells[0].split(",")]
        for path in re.findall(r"`([^`]+)`", cells[1]):
            for m in methods:
                pairs.add((m, path))
    return pairs


def test_the_browser_table_is_the_code():
    documented = _table(_section(), "#### Relayed on the browser gate")
    code = set(peers.BROWSER_ALLOW) | {
        (m, "/mod-store/<modId>") for m in peers.MOD_STORE_METHODS}
    assert documented == code


def test_the_mcp_table_is_the_code():
    documented = _table(_section(), "#### Relayed on the MCP gate")
    assert documented == set(peers.MCP_ALLOW)


def test_the_section_states_what_the_code_does():
    s = _section()
    assert peers.PEER_TOKEN_HEADER in s and peers.PEER_ORIGIN_HEADER in s
    assert peers.SANDBOX_CSP in s
    assert (f"**{peers.PEER_MAX_DIALS_PER_PEER} per peer within "
            f"{peers.PEER_MAX_DIALS} overall**") in s
    assert f"Connect timeout {peers.PEER_CONNECT_TIMEOUT_S:g} s" in s
    assert f"read timeout {peers.PEER_READ_TIMEOUT_S:g} s" in s
    code = ((REPO / "webterm" / "broker" / "app.py").read_text(
        encoding="utf-8")
        + (REPO / "webterm" / "broker" / "peers.py").read_text(
            encoding="utf-8"))
    documented = set(re.findall(r"\b(?:40\d|50\d) `([a-z_]+)`", s))
    assert documented == {"auth_required", "unknown_peer",
                          "proxy_unavailable", "peer_hop_refused",
                          "not_relayed", "peer_origin_changed", "bad_scope",
                          "peer_busy"}
    for code_name in documented:
        assert f'"{code_name}"' in code, code_name
