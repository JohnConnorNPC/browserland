"""#252 phase 1, atom 4: the page side of the peer proxy, run in node.

The shipped fragments are SLICED (by phrase, never copied) and executed with
stubbed ``fetch`` / ``WebSocket`` / ``localStorage``-backed ``prefs``:

* a host record with ``via: 'server'`` + ``peerId`` is fetched through
  ``/peer/<peerId>/`` on this origin, with THIS broker's token as the bearer,
  its own token in the peer-token header and its pairing in the origin header;
  a direct host is untouched;
* the token belongs to (peerId, origin): a re-pointed peer loses it, and a 409
  ``peer_origin_changed`` clears it and reads as a 401;
* a broker that cannot proxy fails the host CLOSED (synthetic 503, "this
  broker cannot proxy", nothing dialed); only an OK ``/info`` is memoized;
* ``hostOriginsForCsp`` skips proxied hosts, ``hostWsUrl`` answers null for
  them and neither dial site constructs a WebSocket;
* a registry row cannot make a host proxied (the host-registry importer drops
  the three fields), and the header names match the server's constants.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import peers, ui

BROKER = Path(ui.__file__).resolve().parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _src(name):
    return (BROKER / name).read_text(encoding="utf-8")


def _func(src, name):
    """One top-level fragment function, from its line to its closing brace
    (the first 8-space-indented ``}`` line after it)."""
    a = src.index("function " + name + "(")
    a = src.rfind("\n", 0, a) + 1                # the start of that line
    b = src.index("\n        }\n", a) + len("\n        }\n")
    return src[a:b]


def _hosts_source():
    s56 = _src("56_js_hosts.js")
    a = s56.index("        function getHosts() {")
    b = s56.index("        // #190: a host added after this page's boot")
    c = s56.index("        const CSP_HOSTS_COOKIE =")
    d = s56.index("        let _cspCookieLast = null;", c)
    return s56[a:b] + s56[c:d]


def _doors_source():
    s63 = _src("63_js_clipboard_auth.js")
    a = s63.index("        const PEER_TOKEN_HEADER =")
    b = s63.index("        // Login overlay, per host")
    return s63[a:b]


PRELUDE = r"""
'use strict';
const FETCH_TIMEOUT_MS = 3000;
const calls = [];
const saves = [];
const invalidated = [];
let infoAnswers = [];
let peerAnswer = null;
const window = { location: { protocol: 'http:', host: 'a:4445' } };
function savePrefs() { saves.push(1); }
function invalidateHost(id) { invalidated.push(id); }
async function fetch(url, o) {
    const h = {};
    if (o && o.headers) o.headers.forEach((v, k) => { h[k] = v; });
    calls.push({ url: url, headers: h });
    if (url === '/info') {
        const next = infoAnswers.length ? infoAnswers.shift() : null;
        if (next === 'reject') throw new TypeError('Failed to fetch');
        if (next === 500) return new Response('{}', { status: 500 });
        return new Response(JSON.stringify(next || {}), { status: 200 });
    }
    if (peerAnswer) {
        const a = peerAnswer;
        if (a.hook) a.hook();
        return new Response(JSON.stringify(a.body), {
            status: a.status, headers: a.headers || {} });
    }
    return new Response('[]', { status: 200 });
}
"""


def _run(tmp_path, prefs, body, extra_src=""):
    script = (PRELUDE + "let prefs = " + json.dumps(prefs) + ";\n"
              + _hosts_source() + _doors_source() + extra_src
              + "(async () => {\n" + body + "\n})().then(out => {\n"
              "  process.stdout.write(JSON.stringify(out) + '\\n');\n"
              "}).catch(e => { process.stdout.write(JSON.stringify("
              "{ threw: String(e) }) + '\\n'); });\n")
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


A_TOKEN = "A-TOKEN"
B_TOKEN = "B-TOKEN"
INFO = {"ok": True, "peers": {"b": "http://b:4448"}, "peer_proxy": True}


def _prefs(**over):
    via = {"id": "rb", "label": "B", "url": "http://b:4448", "token": B_TOKEN,
           "via": "server", "peerId": "b", "peerOrigin": "http://b:4448"}
    via.update(over)
    return {"_hosts": [
        {"id": "local", "token": A_TOKEN},
        via,
        {"id": "rd", "label": "D", "url": "http://d:4445", "token": "D-TOKEN"},
    ]}


BODY_FETCH_BOTH = """
infoAnswers = [INFO];
const r1 = await hostFetch(hostById('rb'), '/sessions');
const r2 = await hostFetch(hostById('rd'), '/sessions');
return { calls: calls, s1: r1.status, s2: r2.status,
         host: hostById('rb') };
""".replace("INFO", json.dumps(INFO))


def test_a_proxied_host_goes_through_the_prefix_with_both_tokens(tmp_path):
    out = _run(tmp_path, _prefs(), BODY_FETCH_BOTH)
    peer_call = [c for c in out["calls"] if c["url"].startswith("/peer/")]
    assert len(peer_call) == 1
    c = peer_call[0]
    assert c["url"] == "/peer/b/sessions"
    assert c["headers"]["authorization"] == "Bearer " + A_TOKEN
    assert c["headers"]["x-browserland-peer-token"] == B_TOKEN
    assert c["headers"]["x-browserland-peer-origin"] == "http://b:4448"
    # Nothing ever dialed B's url.
    assert not [x for x in out["calls"] if "b:4448" in x["url"]]


def test_a_caller_cannot_choose_the_bearer(tmp_path):
    prefs = _prefs()
    prefs["_hosts"][0]["token"] = ""          # this broker: no token stored
    out = _run(tmp_path, prefs, """
infoAnswers = [INFO];
await hostFetch(hostById('rb'), '/sessions',
                { headers: { Authorization: 'Bearer SLIPPED' } });
return calls;
""".replace("INFO", json.dumps(INFO)))
    assert out[-1]["url"] == "/peer/b/sessions"
    assert "authorization" not in out[-1]["headers"]


def test_a_direct_host_is_unchanged(tmp_path):
    out = _run(tmp_path, _prefs(), BODY_FETCH_BOTH)
    d = [c for c in out["calls"] if c["url"].startswith("http://d:4445")]
    assert d == [{"url": "http://d:4445/sessions",
                  "headers": {"authorization": "Bearer D-TOKEN"}}]


def test_the_local_info_is_read_once_when_it_answers(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
await hostFetch(hostById('rb'), '/sessions');
await hostFetch(hostById('rb'), '/info');
return calls;
""".replace("INFO", json.dumps(INFO)))
    assert [c["url"] for c in out] == ["/info", "/peer/b/sessions",
                                       "/peer/b/info"]


@pytest.mark.parametrize("first", ["reject", 500])
def test_a_failed_local_info_is_not_memoized(tmp_path, first):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [FIRST, INFO];
let first = 'resolved';
try {
    const r = await hostFetch(hostById('rb'), '/sessions');
    first = r.status;
} catch (e) { first = 'rejected'; }
const second = await hostFetch(hostById('rb'), '/sessions');
return { first: first, second: second.status, calls: calls };
""".replace("FIRST", json.dumps(first)).replace("INFO", json.dumps(INFO)))
    assert out["first"] == "rejected"
    assert out["second"] == 200
    assert [c["url"] for c in out["calls"]] == ["/info", "/info",
                                                "/peer/b/sessions"]


def test_a_repointed_peer_loses_its_token(tmp_path):
    moved = {"ok": True, "peers": {"b": "http://b2:4448"}, "peer_proxy": True}
    out = _run(tmp_path, _prefs(), """
infoAnswers = [MOVED];
await hostFetch(hostById('rb'), '/sessions');
return { calls: calls, host: hostById('rb'), saves: saves.length,
         invalidated: invalidated };
""".replace("MOVED", json.dumps(moved)))
    host = out["host"]
    assert host["token"] == ""
    assert host["peerOrigin"] == "http://b2:4448"
    assert host["url"] == "http://b2:4448"
    assert out["saves"] >= 1 and out["invalidated"] == ["rb"]
    c = out["calls"][-1]
    assert c["url"] == "/peer/b/sessions"
    assert "x-browserland-peer-token" not in c["headers"]
    assert c["headers"]["x-browserland-peer-origin"] == "http://b2:4448"


def test_an_unpaired_token_is_dropped_too(tmp_path):
    out = _run(tmp_path, _prefs(peerOrigin=""), """
infoAnswers = [INFO];
await hostFetch(hostById('rb'), '/sessions');
return hostById('rb');
""".replace("INFO", json.dumps(INFO)))
    assert out["token"] == "" and out["peerOrigin"] == "http://b:4448"


def test_a_pairing_409_clears_the_token_and_reads_as_401(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
peerAnswer = { status: 409, body: { ok: false,
                                     error: 'peer_origin_changed' },
               headers: { 'X-Browserland-Peer-Refusal':
                          'peer_origin_changed' } };
const r = await hostFetch(hostById('rb'), '/sessions');
const j = await r.json();
return { status: r.status, body: j, host: hostById('rb'),
         infos: calls.filter(c => c.url === '/info').length };
""".replace("INFO", json.dumps(INFO)))
    assert out["status"] == 401
    assert out["body"] == {"ok": False, "error": "auth_required"}
    assert out["host"]["token"] == ""
    assert out["infos"] == 2                      # the memo was dropped
    assert out["host"]["peerOrigin"] == "http://b:4448"   # re-paired


def test_a_409_from_b_itself_is_passed_through(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
peerAnswer = { status: 409, body: { ok: false, error: 'conflict' } };
const r = await hostFetch(hostById('rb'), '/mod-store/clock');
return { status: r.status, host: hostById('rb') };
""".replace("INFO", json.dumps(INFO)))
    assert out["status"] == 409
    assert out["host"]["token"] == B_TOKEN


@pytest.mark.parametrize("info", [
    {"ok": True, "broker_id": "old-build"},                 # predates #252
    {"ok": True, "peers": {"b": "http://b:4448"}, "peer_proxy": False},
    {"ok": True, "peers": {}, "peer_proxy": True},          # not published
])
def test_a_broker_that_cannot_proxy_fails_closed(tmp_path, info):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
const r = await hostFetch(hostById('rb'), '/sessions');
return { status: r.status, body: await r.json(), calls: calls };
""".replace("INFO", json.dumps(info)))
    assert out["status"] == 503
    assert out["body"] == {"ok": False, "error": "proxy_unavailable",
                           "message": "this broker cannot proxy"}
    assert [c["url"] for c in out["calls"]] == ["/info"]


def test_csp_origins_skip_proxied_hosts(tmp_path):
    out = _run(tmp_path, _prefs(), "return hostOriginsForCsp();")
    assert out == ["http://d:4445"]


def test_record_normalization(tmp_path):
    prefs = {"_hosts": [
        {"id": "r1", "url": "http://x:1", "via": "server"},      # no peerId
        {"id": "r2", "url": "http://x:2", "via": "direct", "peerId": "b"},
        {"id": "r3", "url": "http://x:3", "via": "server", "peerId": "b"},
    ]}
    out = _run(tmp_path, prefs, "return getHosts();")
    r1, r2, r3 = out[1], out[2], out[3]
    assert "via" not in r1 and "peerId" not in r1
    assert "via" not in r2 and "peerId" not in r2
    assert (r3["via"], r3["peerId"], r3["peerOrigin"]) == ("server", "b", "")


def test_ws_url_is_null_for_a_proxied_host(tmp_path):
    out = _run(tmp_path, _prefs(), """
return { via: hostWsUrl(hostById('rb'), '/ws?session=1'),
         direct: hostWsUrl(hostById('rd'), '/ws?session=1') };
""")
    assert out["via"] is None
    assert out["direct"] == "ws://d:4445/ws?session=1&token=D-TOKEN"


# ---- the dial sites ----------------------------------------------------------

DIAL_STUBS = r"""
let wsMade = 0;
class WebSocket { constructor(u) { wsMade++; this.url = u; } }
WebSocket.OPEN = 1; WebSocket.CONNECTING = 0;
const CLIENT_ID = 'cid';
const controlSockets = new Map();
const written = [];
function scheduleControlReattach() { written.push('reattach'); }
function closeWindow() {}
function dispatchTermOut() {}
"""


def test_no_websocket_is_made_for_a_proxied_host(tmp_path):
    s64 = _src("64_js_sessions_poll_control.js")
    s73 = _src("73_js_window_runtime.js")
    extra = (DIAL_STUBS + _func(s64, "openControlWs")
             + _func(s64, "_dialControl") + _func(s73, "attachWebSocket"))
    out = _run(tmp_path, _prefs(), """
const host = hostById('rb');
openControlWs(host);
const cand = _dialControl({ hostId: 'rb' }, host, 'cand');
const win = { id: 1, hostId: 'rb', sid: 's1', controlled: false,
              term: { write: (t) => written.push(t) } };
attachWebSocket(win);
attachWebSocket(win);           // a re-dial: still no socket, no 2nd notice
return { wsMade: wsMade, sockets: controlSockets.size, cand: cand,
         written: written, ws: win.ws === undefined };
""", extra_src=extra)
    assert out["wsMade"] == 0
    assert out["sockets"] == 0
    assert out["cand"] is None
    assert out["written"] == ["proxied terminals arrive in the next phase\r\n"]
    assert out["ws"] is True


def test_identity_refresh_probes_through_the_branch(tmp_path):
    s83 = _src("83_js_broker_identity.js")
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
peerAnswer = { status: 200, body: { ok: true, broker_id: 'B-ID' } };
const h = hostById('rb');
const id = await probeBrokerId(h.url, h.token, undefined, h);
return { id: id, calls: calls };
""".replace("INFO", json.dumps(INFO)), extra_src=_func(s83, "probeBrokerId"))
    assert out["id"] == "B-ID"
    assert [c["url"] for c in out["calls"]] == ["/info", "/peer/b/info"]
    assert re.search(r"probeBrokerId\(h\.url, h\.token,\s+undefined, h\)",
                     _func(s83, "refreshHostIdentities"))


def test_the_login_probe_goes_through_the_branch():
    s63 = _src("63_js_clipboard_auth.js")
    a = s63.index("document.getElementById('auth-form')")
    b = s63.index("host.token = candidate;", a)
    submit = s63[a:b + 200]
    assert "hostFetch({ url: host.url, token: candidate }" not in submit
    assert re.search(r"if \(host\.via === 'server'\) \{\s+const info = "
                     r"await localPeers\(\);", submit)
    assert "probe = Object.assign({}, host, {" in submit
    assert "resp = await hostFetch(probe, '/sessions');" in submit
    assert ("if (host.via === 'server') host.peerOrigin = probe.peerOrigin;"
            in submit)


# ---- a registry row cannot make a host proxied (S5) ----------------------------

def test_a_registry_row_cannot_set_via_or_peer_id(tmp_path):
    src = (BROKER / "mods" / "host-registry" / "host-registry.js").read_text(
        encoding="utf-8")
    a = src.index("function normEntry(raw) {")
    b = src.index("\n                }\n", a) + len("\n                }\n")
    script = ("'use strict';\nconst URL_MAX = 2048, LABEL_MAX = 64, "
              "TOKEN_MAX = 512;\n"
              "function cleanStr(v, n) { return typeof v === 'string' ? "
              "v.slice(0, n) : ''; }\n"
              "function cleanColor() { return ''; }\n"
              "function normalizeHostUrl(u) { return u; }\n"
              + src[a:b] +
              "process.stdout.write(JSON.stringify(normEntry({ id: 'rb', "
              "url: 'http://b:1', via: 'server', peerId: 'b', "
              "peerOrigin: 'http://evil:1' })) + '\\n');\n")
    path = tmp_path / "reg.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    entry = json.loads(proc.stdout)
    assert entry["url"] == "http://b:1"
    for key in ("via", "peerId", "peerOrigin"):
        assert key not in entry


# ---- one spelling of each header name (S9) ------------------------------------

def test_header_names_match_the_server():
    s63 = _src("63_js_clipboard_auth.js")
    assert f"const PEER_TOKEN_HEADER = '{peers.PEER_TOKEN_HEADER}';" in s63
    assert f"const PEER_ORIGIN_HEADER = '{peers.PEER_ORIGIN_HEADER}';" in s63
    page = "".join(_src(n) for n in ui._ORDERED if n.endswith(".js"))
    assert (f"const PEER_REFUSAL_HEADER = '{peers.PEER_REFUSAL_HEADER}';"
            in s63)
    for name in (peers.PEER_TOKEN_HEADER, peers.PEER_ORIGIN_HEADER,
                 peers.PEER_REFUSAL_HEADER):
        assert page.count(name) == 1, name


def test_a_peer_cannot_forge_the_pairing_refusal(tmp_path):
    """B writes B's bodies: the reserved error string in a relayed 409,
    without THIS broker's marker header, must not cost the token."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
peerAnswer = { status: 409, body: { ok: false,
                                     error: 'peer_origin_changed' } };
const r = await hostFetch(hostById('rb'), '/sessions');
return { status: r.status, host: hostById('rb') };
""".replace("INFO", json.dumps(INFO)))
    assert out["status"] == 409
    assert out["host"]["token"] == B_TOKEN


def test_a_stale_refusal_cannot_erase_a_newer_password(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
peerAnswer = { status: 409, body: {},
               headers: { 'X-Browserland-Peer-Refusal':
                          'peer_origin_changed' },
               // the user re-entered the password while this was in flight
               hook: () => { hostById('rb').token = 'NEWER'; } };
const r = await hostFetch(hostById('rb'), '/sessions');
return { status: r.status, host: hostById('rb') };
""".replace("INFO", json.dumps(INFO)))
    assert out["status"] == 409
    assert out["host"]["token"] == "NEWER"


def test_the_local_host_is_never_proxied(tmp_path):
    prefs = _prefs()
    prefs["_hosts"][0].update(via="server", peerId="b", peerOrigin="x")
    out = _run(tmp_path, prefs, """
infoAnswers = [INFO];
const local = localHost();
const r = await hostFetch(local, '/info');
return { local: local, status: r.status, calls: calls };
""".replace("INFO", json.dumps(INFO)))
    for key in ("via", "peerId", "peerOrigin"):
        assert key not in out["local"]
    assert out["status"] == 200
    assert [c["url"] for c in out["calls"]] == ["/info"]



def test_a_cannot_proxy_answer_is_asked_again(tmp_path):
    """NIT 8: the synthetic 503 drops the memo, so a peer added to this
    broker after the page loaded is used on the next call."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [{ ok: true, peers: {}, peer_proxy: true }, INFO];
const r1 = await hostFetch(hostById('rb'), '/sessions');
const r2 = await hostFetch(hostById('rb'), '/sessions');
return { s1: r1.status, s2: r2.status, calls: calls.map(c => c.url) };
""".replace("INFO", json.dumps(INFO)))
    assert out["s1"] == 503 and out["s2"] == 200
    assert out["calls"] == ["/info", "/info", "/peer/b/sessions"]
