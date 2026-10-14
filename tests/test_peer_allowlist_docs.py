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

from webterm.broker import peer_sockets, peers

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
    assert (f"**{peers.PEER_MCP_MAX_DIALS_PER_PEER} per peer within "
            f"{peers.PEER_MCP_MAX_DIALS}**") in s
    assert f"**{peers.PEER_DIAL_MAX_S:g} s**" in s
    assert f"at most {peers.PEER_MCP_MAX_BODY // 2**20} MiB, within " \
        f"{peers.PEER_MCP_BODY_DEADLINE_S:g} s" in s
    assert f"Connect timeout {peers.PEER_CONNECT_TIMEOUT_S:g} s" in s
    assert f"read timeout {peers.PEER_READ_TIMEOUT_S:g} s" in s
    code = ((REPO / "webterm" / "broker" / "app.py").read_text(
        encoding="utf-8")
        + (REPO / "webterm" / "broker" / "peers.py").read_text(
            encoding="utf-8"))
    documented = set(re.findall(r"\b(?:4\d\d|5\d\d) `([a-z_]+)`", s))
    assert documented == {"auth_required", "unknown_peer",
                          "proxy_unavailable", "peer_hop_refused",
                          "not_relayed", "peer_origin_changed", "bad_scope",
                          "peer_busy", "body_too_large", "bad_length",
                          "body_timeout", "peer_timeout", "bad_ticket"}
    for code_name in documented:
        assert f'"{code_name}"' in code, code_name


def test_the_sockets_part_states_what_the_code_does():
    # #252 phase 2: every number of the ticket and socket scheme, from the
    # code's own constants.
    s = _section()
    ps = peer_sockets
    assert "#### Sockets" in s
    assert f"lives **{ps.PEER_TICKET_TTL_S:g} s**" in s
    assert f'"ttl": {ps.PEER_TICKET_TTL_S:g}' in s
    assert (f"**{ps.PEER_MAX_TICKETS_PER_PEER} per peer within "
            f"{ps.PEER_MAX_TICKETS} tickets**") in s
    assert (f"**{ps.PEER_MAX_SOCKETS_PER_PEER} per peer within "
            f"{ps.PEER_MAX_SOCKETS} sockets**") in s
    assert "1000 `idle`" not in s and "Idle ceiling" not in s   # 1b
    kw = ps.UPSTREAM_WS_KWARGS
    flat = " ".join(s.split())          # the prose wraps anywhere
    assert f"at most {kw['max_queue']} messages buffered" in flat
    assert (f"Each leg is pinged every {ps.PEER_WS_PING_S:g} s and dropped "
            f"after {ps.PEER_WS_PING_S:g} s without an answer") in flat
    assert "The server-side handshake never follows a redirect" in flat
    assert f"`websockets` {peers.PEER_WS_MIN_MAJOR} or later" in flat
    assert "until phase 2" not in s and "Phase 1 limits" not in s
    # Review round 1b: the page's mint bound, from the page's own constant.
    js = (REPO / "webterm" / "broker" / "63_js_clipboard_auth.js").read_text(
        encoding="utf-8")
    bound = int(re.search(r"const PEER_MINTS_PER_HOST = (\d+);", js).group(1))
    assert f"The page mints at most {bound} tickets per host at a time" in flat
    # Review round 1b: A's own refusals are 4403; a 4401 is the peer's.
    assert "| %d `ticket` |" % ps.PEER_TICKET_REFUSED in s
    assert "| %d `revoked` |" % ps.PEER_TICKET_REFUSED in s
    assert "4401 `ticket`" not in s and "4401 `revoked`" not in s
