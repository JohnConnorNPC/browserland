"""#252 phase 1 atom 4 and phase 2 atom 4: the page side of the peer proxy, run in node.

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
* ``hostOriginsForCsp`` skips proxied hosts; ``hostWsUrl`` answers a direct
  host's url at once and a proxied host's as a promise (a one-time ticket
  minted through ``/peer/<id>/ws-ticket``), and both dial sites dial what it
  resolves -- or show the overlay (401) or the chip's reason and back off
  (anything else) (#252 phase 2);
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
let peerInflight = 0, peerInflightMax = 0;
const window = { location: { protocol: 'http:', host: 'a:4445' } };
function savePrefs() { saves.push(1); }
function invalidateHost(id) { invalidated.push(id); }
async function fetch(url, o) {
    const h = {};
    if (o && o.headers) o.headers.forEach((v, k) => { h[k] = v; });
    calls.push({ url: url, headers: h, method: o && o.method,
                 body: o && o.body });
    if (url === '/info') {
        const next = infoAnswers.length ? infoAnswers.shift() : null;
        if (next === 'reject') throw new TypeError('Failed to fetch');
        if (next === 500) return new Response('{}', { status: 500 });
        return new Response(JSON.stringify(next || {}), { status: 200 });
    }
    if (peerAnswer) {
        const a = peerAnswer;
        if (a.hook) a.hook();
        if (a.delay) {
            peerInflight += 1;
            peerInflightMax = Math.max(peerInflightMax, peerInflight);
            await new Promise(r => setTimeout(r, a.delay));
            peerInflight -= 1;
        }
        if (a.stall) {
            // The headers arrive; the body never does.
            return { status: a.status, ok: a.status >= 200 && a.status < 300,
                     headers: new Headers(a.headers || {}),
                     json: () => new Promise(() => {}) };
        }
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
                          encoding="utf-8",
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


def test_ws_url_is_sync_for_a_direct_host_and_a_promise_through_a_peer(
        tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
peerAnswer = { status: 200, body: { ok: true, ticket: 'T/1+', ttl: 30 } };
const direct = hostWsUrl(hostById('rd'), '/ws?session=1');
const p = hostWsUrl(hostById('rb'), '/ws?session=1&clientId=cid');
const isPromise = typeof p.then === 'function';
const via = await p;
const ctl = await hostWsUrl(hostById('rb'), '/control?clientId=cid');
return { direct: direct, isPromise: isPromise, via: via, ctl: ctl,
         calls: calls.filter(c => c.url !== '/info') };
""".replace("INFO", json.dumps(INFO)), extra_src=VIA_STUBS)
    assert out["direct"] == "ws://d:4445/ws?session=1&token=D-TOKEN"
    assert out["isPromise"] is True
    # Same origin, the ticket and nothing else: no token on the url.
    assert out["via"] == ("ws://a:4445/peer/b/ws?session=1&clientId=cid"
                          "&ticket=T%2F1%2B")
    assert out["ctl"] == "ws://a:4445/peer/b/control?clientId=cid&ticket=T%2F1%2B"
    assert "token" not in out["via"] and B_TOKEN not in out["via"]
    mints = out["calls"]
    assert [c["url"] for c in mints] == ["/peer/b/ws-ticket"] * 2
    for c, route in zip(mints, ("ws", "control")):
        assert c["method"] == "POST"
        assert json.loads(c["body"]) == {"route": route, "clientId": "cid"}
        assert c["headers"]["authorization"] == "Bearer " + A_TOKEN
        assert c["headers"]["x-browserland-peer-token"] == B_TOKEN
        assert c["headers"]["x-browserland-peer-origin"] == "http://b:4448"


# ---- the dial sites ----------------------------------------------------------

VIA_STUBS = r"""
let wsMade = 0;
class WebSocket { constructor(u) { wsMade++; this.url = u; } }
WebSocket.OPEN = 1; WebSocket.CONNECTING = 0;
const CLIENT_ID = 'cid';
const WS_CONNECT_TIMEOUT_MS = 1;
const controlSockets = new Map();
const _ctlSockState = new WeakMap();
function _armStatusWatchdog() {}
const written = [];
const overlays = [];
const reattached = [];
let renders = 0;
const polls = new Map();
function pollStateFor(id) {
    if (!polls.has(id)) polls.set(id, { authNeeded: false });
    return polls.get(id);
}
function showAuthOverlay(h) { overlays.push(h && h.id); }
function renderHostStatus() { renders++; }
function scheduleReattach(w) { reattached.push('win'); }
function scheduleControlReattach(id) { reattached.push('ctl:' + id); }
function closeWindow() {}
function dispatchTermOut(w, t) { written.push(t); }
function hostMenuState() { return 'ok'; }
function _retireControlSocket() {}
function homeHostId() { return 'local'; }
function _armLostContact() {}
function _clearLostContact() {}
const settle = async () => { for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 0)); } };
function mkWin(hid) {
    return { id: 1, hostId: hid, sid: 's1', controlled: false,
             term: { write: (t) => written.push(t) } };
}
"""


def _dial_src():
    s64 = _src("64_js_sessions_poll_control.js")
    s73 = _src("73_js_window_runtime.js")
    s75 = _src("75_js_taskbar_hosts.js")
    return (VIA_STUBS + _func(s64, "openControlWs")
            + _func(s64, "_dialControl") + _func(s64, "closeControlWs")
            + _func(s64, "_controlSocketGone")
            + _func(s73, "attachWebSocket")
            + _func(s75, "hostNeedsAttention") + _func(s75, "hostChipTip"))


def _dial(tmp_path, body, answer=None, info=INFO):
    pre = "infoAnswers = [INFO, INFO, INFO];\n".replace(
        "INFO", json.dumps(info))
    if answer is not None:
        pre += "peerAnswer = " + json.dumps(answer) + ";\n"
    return _run(tmp_path, _prefs(), pre + body, extra_src=_dial_src())


OK_TICKET = {"status": 200, "body": {"ok": True, "ticket": "T", "ttl": 30}}
BUSY = {"status": 503, "body": {"ok": False, "error": "peer_busy"},
        "headers": {"X-Browserland-Peer-Refusal": "peer_busy"}}


def test_a_direct_host_dials_on_the_same_tick(tmp_path):
    out = _dial(tmp_path, """
const win = mkWin('rd');
attachWebSocket(win);
const sync = wsMade;
openControlWs(hostById('rd'));
return { sync: sync, made: wsMade, url: win.ws.url,
         ctl: controlSockets.get('rd').ws.url };
""")
    assert out["sync"] == 1 and out["made"] == 2
    assert out["url"] == "ws://d:4445/ws?session=s1&clientId=cid&token=D-TOKEN"
    assert out["ctl"] == "ws://d:4445/control?clientId=cid&token=D-TOKEN"


def test_both_sites_dial_the_ticketed_url(tmp_path):
    out = _dial(tmp_path, """
const win = mkWin('rb');
attachWebSocket(win);
attachWebSocket(win);              // a re-dial during the mint
openControlWs(hostById('rb'));
openControlWs(hostById('rb'));     // the poll loop's next tick
const during = wsMade;
await settle();
const rec = controlSockets.get('rb');
return { during: during, made: wsMade, url: win.ws.url, ctl: rec.ws.url,
         inState: _ctlSockState.has(rec.ws), minting: [!!win.minting,
         !!rec.minting], mints: calls.filter(
             c => c.url === '/peer/b/ws-ticket').length,
         written: written, reattached: reattached };
""", OK_TICKET)
    assert out["during"] == 0
    assert out["made"] == 2
    assert out["mints"] == 2                       # one per site, not four
    assert out["url"] == "ws://a:4445/peer/b/ws?session=s1&clientId=cid&ticket=T"
    assert out["ctl"] == "ws://a:4445/peer/b/control?clientId=cid&ticket=T"
    assert out["inState"] is True
    assert out["minting"] == [False, False]
    # The window is told nothing but the dial banner (the phase-1 notice is
    # gone), and nothing backs off.
    assert out["written"] == ["connecting to B…"] * 2
    assert out["reattached"] == []


def test_a_mint_401_opens_the_overlay_and_dials_nothing(tmp_path):
    out = _dial(tmp_path, """
const win = mkWin('rb');
attachWebSocket(win);
await settle();
openControlWs(hostById('rb'));
await settle();
return { made: wsMade, auth: pollStateFor('rb').authNeeded,
         overlays: overlays, renders: renders, failed: win.authFailed,
         reattached: reattached, written: written };
""", {"status": 401, "body": {"ok": False, "error": "auth_required"}})
    assert out["made"] == 0
    assert out["auth"] is True
    assert out["overlays"] == ["rb", "rb"]
    assert out["renders"] >= 2
    assert out["failed"] is True           # the healing loop leaves it alone
    assert out["reattached"] == []
    assert out["written"] == []


def test_a_busy_peer_shows_on_the_chip_and_backs_off(tmp_path):
    out = _dial(tmp_path, """
const host = hostById('rb');
const win = mkWin('rb');
attachWebSocket(win);
await settle();
openControlWs(host);
await settle();
const rec = { hostId: 'rb', cur: { ws: null }, cand: null, closedByUs: false };
controlSockets.set('rb', rec);
const cand = _dialControl(rec, host, 'cand');
await settle();
const busy = { refusal: pollStateFor('rb').wsRefusal,
               tip: hostChipTip(host, 'ok'),
               attention: hostNeedsAttention(host) };
peerAnswer = OK;
attachWebSocket(win);
await settle();
return { busy: busy, cand: cand, made: wsMade, reattached: reattached,
         after: pollStateFor('rb').wsRefusal || null,
         attentionAfter: hostNeedsAttention(host),
         tipAfter: hostChipTip(host, 'ok') };
""".replace("OK", json.dumps(OK_TICKET)), BUSY)
    assert out["busy"]["refusal"] == "peer_busy"
    assert out["busy"]["tip"].endswith(" — terminals: the peer is busy")
    assert out["busy"]["attention"] is True
    assert out["cand"] is None
    # 73 backs off once, the socket in charge once, the replacement never.
    assert out["reattached"] == ["win", "ctl:rb"]
    assert out["made"] == 1
    assert out["after"] is None
    assert out["attentionAfter"] is False
    assert "terminals:" not in out["tipAfter"]


@pytest.mark.parametrize("answer, info, phrase", [
    (None, {"ok": True, "peers": {}, "peer_proxy": False},
     "this broker cannot proxy"),
    ({"status": 500, "body": {}}, INFO, "could not connect"),
    ("throw", INFO, "could not connect"),
])
def test_other_mint_failures_name_their_reason(tmp_path, answer, info,
                                               phrase):
    body = """
const host = hostById('rb');
const win = mkWin('rb');
attachWebSocket(win);
await settle();
return { made: wsMade, tip: hostChipTip(host, 'ok'), reattached: reattached };
"""
    if answer == "throw":
        body = "peerAnswer = { hook: () => { throw new TypeError('x'); } };\n" \
            + body
        answer = None
    out = _dial(tmp_path, body, answer, info)
    assert out["made"] == 0
    assert out["tip"].endswith(" — terminals: " + phrase)
    assert out["reattached"] == ["win"]


def test_a_control_record_closed_during_the_mint_is_not_dialed(tmp_path):
    # Review round 1 SHOULD 1: the host was removed (or its cache
    # invalidated) while its /control ticket was being minted. The answer
    # must not dial a socket no record owns.
    out = _dial(tmp_path, """
openControlWs(hostById('rb'));
const minting = !!(controlSockets.get('rb') || {}).minting;
closeControlWs('rb');
await settle();
return { minting: minting, made: wsMade, records: controlSockets.size,
         reattached: reattached,
         mints: calls.filter(c => c.url === '/peer/b/ws-ticket').length };
""", OK_TICKET)
    assert out["minting"] is True
    assert out["mints"] == 1
    assert out["made"] == 0
    assert out["records"] == 0
    assert out["reattached"] == []


def test_a_mint_whose_body_stalls_backs_off(tmp_path):
    # Review round 1 (Q25): fetch() resolves on the headers, so the body
    # read races FETCH_TIMEOUT_MS; a stalled one is a failed mint.
    out = _dial(tmp_path, """
const win = mkWin('rb');
attachWebSocket(win);
await settle();
const during = !!win.minting;
await new Promise(r => setTimeout(r, FETCH_TIMEOUT_MS + 300));
return { during: during, winMinting: !!win.minting, made: wsMade,
         reattached: reattached, refusal: pollStateFor('rb').wsRefusal };
""", {"status": 200, "stall": True})
    assert out["during"] is True
    assert out["winMinting"] is False
    assert out["reattached"] == ["win"]
    assert out["made"] == 0
    assert out["refusal"] == "error"


THREE_CLOSES = """
const mints = () => calls.filter(c => c.url.endsWith('/ws-ticket')).length;
"""


def test_a_4403_re_mints_once_then_backs_off(tmp_path):
    # Review round 1b: THIS broker's refusal (4403) is not the peer's
    # password. One immediate re-mint, then the ordinary back-off.
    out = _dial(tmp_path, THREE_CLOSES + """
const win = mkWin('rb');
attachWebSocket(win);
await settle();
const first = win.ws;
first.onclose({ code: 4403, reason: 'ticket' });
await settle();
const second = win.ws;
const afterOne = { made: wsMade, mints: mints(),
                   reattached: reattached.slice() };
second.onclose({ code: 4403, reason: 'ticket' });
await settle();
return { afterOne: afterOne, renewed: first !== second, made: wsMade,
         mints: mints(), reattached: reattached, overlays: overlays,
         failed: !!win.authFailed };
""", OK_TICKET)
    assert out["afterOne"] == {"made": 2, "mints": 2, "reattached": []}
    assert out["renewed"] is True
    assert (out["made"], out["mints"]) == (2, 2)
    assert out["reattached"] == ["win"]
    assert out["overlays"] == [] and out["failed"] is False


def test_a_4403_after_the_peer_spoke_re_mints_again(tmp_path):
    out = _dial(tmp_path, THREE_CLOSES + """
const win = mkWin('rb');
attachWebSocket(win);
await settle();
win.ws.onclose({ code: 4403, reason: 'ticket' });
await settle();
win.ws.onmessage({ data: new ArrayBuffer(1) });   // the peer answered
win.ws.onclose({ code: 4403, reason: 'revoked' });
await settle();
return { made: wsMade, mints: mints(), reattached: reattached };
""", OK_TICKET)
    assert (out["made"], out["mints"]) == (3, 3)
    assert out["reattached"] == []


def test_the_peers_own_4401_still_opens_the_overlay(tmp_path):
    out = _dial(tmp_path, THREE_CLOSES + """
const win = mkWin('rb');
attachWebSocket(win);
await settle();
win.ws.onclose({ code: 4401, reason: 'auth required' });
await settle();
openControlWs(hostById('rb'));
await settle();
controlSockets.get('rb').ws.onclose({ code: 4401, reason: 'auth required' });
await settle();
return { made: wsMade, mints: mints(), overlays: overlays,
         failed: !!win.authFailed, reattached: reattached };
""", OK_TICKET)
    assert out["overlays"] == ["rb", "rb"]
    assert out["failed"] is True
    assert (out["made"], out["mints"]) == (2, 2)
    assert out["reattached"] == []


def test_a_control_4403_re_mints_once_then_backs_off(tmp_path):
    out = _dial(tmp_path, THREE_CLOSES + """
openControlWs(hostById('rb'));
await settle();
const rec = controlSockets.get('rb');
rec.ws.onclose({ code: 4403, reason: 'ticket' });
await settle();
const afterOne = { made: wsMade, mints: mints(),
                   reattached: reattached.slice() };
rec.ws.onmessage({ data: 'not json' });         // the peer answered
rec.ws.onclose({ code: 4403, reason: 'revoked' });
await settle();
const afterReset = { made: wsMade, reattached: reattached.slice() };
rec.ws.onclose({ code: 4403, reason: 'ticket' });
await settle();
return { afterOne: afterOne, afterReset: afterReset, made: wsMade,
         mints: mints(), reattached: reattached, overlays: overlays };
""", OK_TICKET)
    assert out["afterOne"] == {"made": 2, "mints": 2, "reattached": []}
    assert out["afterReset"] == {"made": 3, "reattached": []}
    assert (out["made"], out["mints"]) == (3, 3)
    assert out["reattached"] == ["ctl:rb"]
    assert out["overlays"] == []


def test_a_burst_of_windows_mints_at_most_4_at_a_time(tmp_path):
    # Review round 1b: 20 windows re-dial in one tick (a restore, or the
    # healing loop after the peer restarted): never more than 4 mints in
    # flight for the host, every window still gets its own and dials.
    answer = dict(OK_TICKET, delay=30)
    out = _dial(tmp_path, """
const wins = [];
for (let i = 0; i < 20; i++) {
    const w = mkWin('rb');
    w.id = i; w.sid = 's' + i;
    wins.push(w);
    attachWebSocket(w);
}
await new Promise(r => setTimeout(r, 1500));
return { made: wsMade, max: peerInflightMax,
         mints: calls.filter(c => c.url === '/peer/b/ws-ticket').length,
         dialed: wins.filter(w => w.ws && /ticket=T$/.test(w.ws.url)).length,
         sids: wins.map(w => (w.ws.url.match(/session=(s\\d+)/) || [])[1]),
         minting: wins.filter(w => w.minting).length };
""", answer)
    assert out["max"] == 4
    assert out["mints"] == 20 and out["made"] == 20 and out["dialed"] == 20
    assert out["sids"] == ["s%d" % i for i in range(20)]
    assert out["minting"] == 0


def test_a_window_closed_during_the_mint_is_not_dialed(tmp_path):
    out = _dial(tmp_path, """
const win = mkWin('rb');
attachWebSocket(win);
win.disposed = true;
await settle();
return { made: wsMade, minting: !!win.minting };
""", OK_TICKET)
    assert out == {"made": 0, "minting": False}


def test_a_ticket_a_guard_turned_away_is_never_reused(tmp_path):
    out = _dial(tmp_path, """
const win = mkWin('rb');
attachWebSocket(win);
win.controlled = { label: 'phone' };     // taken over during the mint
await settle();
const held = wsMade;
win.controlled = false;
peerAnswer = { status: 200, body: { ok: true, ticket: 'T2', ttl: 30 } };
attachWebSocket(win);
await settle();
return { held: held, made: wsMade, url: win.ws.url, mints: calls.filter(
    c => c.url === '/peer/b/ws-ticket').length };
""", OK_TICKET)
    assert out["held"] == 0
    assert out["mints"] == 2
    assert out["made"] == 1
    assert out["url"].endswith("&ticket=T2")


def test_the_phase_1_notice_is_gone():
    for name in ("73_js_window_runtime.js", "64_js_sessions_poll_control.js",
                 "63_js_clipboard_auth.js"):
        src = _src(name)
        assert "proxyNoticeShown" not in src
        assert "arrive in the next phase" not in src


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
                          encoding="utf-8",
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
