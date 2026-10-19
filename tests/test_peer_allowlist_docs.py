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
    assert f"**{peers.PEER_RECORDING_DIAL_MAX_S:g} s**" in s
    # #252 phase 3: the MCP gate's row names its pairing refusal, in place:
    # after the table (round 1b S1), so 404 `not_relayed` comes first.
    mcp_row = next(line for line in s.splitlines()
                   if line.startswith("| `/peer/<id>/mcp/*` |"))
    assert ("404 `not_relayed` → **409 `peer_origin_changed`**"
            in mcp_row)
    assert mcp_row.index("409 `peer_origin_changed`") > mcp_row.index(
        "404 `not_relayed`")
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
                          "body_timeout", "peer_timeout", "peer_unreachable",
                          "bad_ticket", "hub_credential_refused"}
    for code_name in documented:
        assert f'"{code_name}"' in code, code_name
    # #260 review round 1: the name alone also matches app.py's WS close
    # reason, so the relay's own 502 is pinned as a pair, doc and code.
    pairs = set(re.findall(r"\b(4\d\d|5\d\d) `([a-z_]+)`", s))
    assert ("502", "peer_unreachable") in pairs, sorted(pairs)
    assert 'return 502, "peer_unreachable"' in (
        REPO / "webterm" / "broker" / "peers.py").read_text(encoding="utf-8")


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


# #261: the refusals the hub's MCP relay adds, as an agent's client
# must be able to explain them. Spelled out here, not derived from the wiki
# row, so deleting a code from both the wiki and the client table still fails.
RELAY_CODES = {"unknown_peer", "proxy_unavailable", "peer_busy",
               "peer_timeout", "peer_unreachable", "not_relayed",
               "peer_hop_refused", "body_too_large", "body_timeout",
               "bad_length", "hub_credential_refused"}


def test_the_relay_code_set_is_texted_and_documented():
    """The section's documented codes, less the ones that are not new relay
    refusals (bad_ticket is the browser gate's; the other three already had
    client texts), are exactly RELAY_CODES, and each has a client text."""
    from webterm.mcptool.client import _ERROR_MESSAGES
    documented = set(re.findall(r"\b(?:4\d\d|5\d\d) `([a-z_]+)`",
                                _section()))
    relay = documented - {"bad_ticket", "auth_required", "bad_scope",
                          "peer_origin_changed"}
    assert relay == RELAY_CODES, sorted(relay ^ RELAY_CODES)
    missing = sorted(c for c in RELAY_CODES if c not in _ERROR_MESSAGES)
    assert not missing, f"no client text for {missing}"


# #261: what the docs tell an agent about a hub and its peers. The sentences
# are matched after joining wrapped lines and dropping code ticks.

_HUB_ALONE = {
    "wiki/MCP-and-AI-Agents.md":
        "An MCP config with only the hub's entry shows only the hub's own "
        "windows",
    "webterm/mcptool/README.md":
        "An MCP config with only the hub's entry shows only the hub's own "
        "windows",
    "wiki/Hosts-and-Multi-Browser.md":
        "An MCP config with only this broker's entry shows only this "
        "broker's own windows",
    "README.md": "shows only that broker's own windows",
}


def _flat(text):
    return " ".join(text.replace("`", "").replace("**", "").split())


def _doc(rel):
    return _flat((REPO / rel).read_text(encoding="utf-8"))


def _corpus_text():
    import json

    def strings(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, dict):
            for v in x.values():
                yield from strings(v)
        elif isinstance(x, list):
            for v in x:
                yield from strings(v)
    raw = json.loads((REPO / "webterm" / "broker" /
                      "help_corpus.json").read_text(encoding="utf-8"))
    return _flat(" ".join(strings(raw)))


def test_the_hub_alone_is_documented():
    for rel, sentence in _HUB_ALONE.items():
        assert sentence in _doc(rel), rel
    # the in-app guide is generated from the wiki: it says it too
    corpus = _corpus_text()
    for rel in ("wiki/MCP-and-AI-Agents.md", "wiki/Hosts-and-Multi-Browser.md"):
        assert _HUB_ALONE[rel] in corpus, rel


def test_the_peer_entry_rules_are_documented_with_the_hubs_numbers():
    from webterm.broker.app import MAX_MCP_WAIT_MS
    for rel in ("wiki/MCP-and-AI-Agents.md", "webterm/mcptool/README.md"):
        text = _doc(rel)
        for phrase in (
                "reaches every entry that names no scope of its own, peer "
                "entries included",
                "writes the hub entry's scope only",
                f"at most {peers.PEER_MCP_MAX_DIALS_PER_PEER} calls per peer "
                f"({peers.PEER_MCP_MAX_DIALS} in all)",
                f"up to {MAX_MCP_WAIT_MS // 1000} s",
                "never queued",
                "<paste this peer's MCP token>",
                "token_placeholder", "unknown_peer", "proxy_unavailable",
                "peer_busy", "peer_hop_refused",
                "launch_terminal needs host=",
                "Include peer entries",
                "off by default: unticked, the file is what it has always been",
                "Peer entries come only from the hub's own tab",
                "never hands its own credentials to a peer",
                "hub_credential_refused",
                "an older mcptool would send it as a bearer",
                "2 of 5 keys sent",
                "who has to act",
                "browser that holds the peer's control lease",
                "no browser on the peer the answer is no_active_browser",
                "carries home: false",
                "workspaces op needs that peer's admin token on the entry",
                "with it, the op answers not_home",
                "take the same relayed route to the peer unchanged",
                "even for a peer that was given the same token as the hub",
                "give that peer its own MCP token",
                "after a peer_timeout it may have been typed, so read the "
                "screen first",
                "delete the entry if you do not use that peer"):
            assert phrase in text, (rel, phrase)
        # arrange_window through a peer was never run live: the docs do not
        # say it goes to the peer's lease holder, only that it is relayed
        assert "get_desktop and arrange_window" not in text, rel


_RELAY_STATUS = {
    "unknown_peer": 404, "proxy_unavailable": 503, "peer_busy": 503,
    "peer_timeout": 504, "peer_unreachable": 502, "not_relayed": 404,
    "peer_hop_refused": 404, "body_too_large": 413, "body_timeout": 408,
    "bad_length": 400, "hub_credential_refused": 403}


def _mcp_error_table():
    text = (REPO / "wiki" / "Technical-Reference.md").read_text(
        encoding="utf-8")
    a = text.index("## MCP HTTP interface")
    return text[a:text.index("### Admin surface", a)]


def test_every_relay_code_has_its_row_in_the_mcp_error_table():
    """#261: client.py names this table as its source of truth, so each of
    relay refusal in RELAY_CODES has a row with its status, and a status the broker
    actually returns (not_relayed and peer_hop_refused get theirs from the
    classify table, so only their rows are pinned)."""
    assert set(_RELAY_STATUS) == RELAY_CODES
    table = _mcp_error_table()
    code = ((REPO / "webterm" / "broker" / "app.py").read_text(
        encoding="utf-8")
        + (REPO / "webterm" / "broker" / "peers.py").read_text(
            encoding="utf-8"))
    for name, status in _RELAY_STATUS.items():
        assert f"| {status} | `{name}` |" in table, name
        if name not in ("not_relayed", "peer_hop_refused"):
            assert re.search(rf'\b{status},\s*"{name}"', code), (name, status)


def test_a_client_only_code_is_not_a_wire_row():
    """token_placeholder is raised by the mcptool, never sent: it is listed
    apart from the table's HTTP rows."""
    table = _mcp_error_table()
    assert "Raised by the `webterm.mcptool` client itself" in table
    assert "`token_placeholder`" in table
    rows = [ln for ln in table.splitlines() if ln.startswith("|")]
    assert not [ln for ln in rows if "token_placeholder" in ln]



def test_upgrading_and_the_changelog_say_a_peer_needs_its_own_token():
    """#261 review: a hub and peer that shared one MCP token relayed before and
    are refused now, so the page an owner reads on upgrading says so."""
    upg = _doc("wiki/Upgrading.md")
    assert "## A peer's MCP token must differ from the hub's (#261)" in upg
    for phrase in ("answers 403 hub_credential_refused and nothing is "
                   "forwarded",
                   "Give the peer its own MCP token",
                   "only an exact match is refused"):
        assert phrase in upg, phrase
    log = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    one_zero = log[log.index("## [1.0.0]"):log.index("## [0.8.0]")]
    assert "**A peer behind a hub needs its own MCP token** (#261)" in one_zero


def test_the_peer_proxy_section_says_a_shared_token_is_refused():
    assert "a peer configured with the same token is refused too" in \
        " ".join(_section().split())
