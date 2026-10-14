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
const window = { location: { protocol: 'http:', host: 'a:4445',
                             origin: 'http://a:4445' } };
function savePrefs() { saves.push(1); }
function invalidateHost(id) { invalidated.push(id); }
async function fetch(url, o) {
    const h = {};
    if (o && o.headers) o.headers.forEach((v, k) => { h[k] = v; });
    calls.push({ url: url, headers: h, method: o && o.method,
                 body: o && o.body });
    if (url === '/info') {
        const next = infoAnswers.length ? infoAnswers.shift() : null;
        if (next && next.__delay) {
            await new Promise(r => setTimeout(r, next.__delay));
        }
        if (next === 'reject') throw new TypeError('Failed to fetch');
        if (typeof next === 'number') {
            return new Response('{}', { status: next });
        }
        return new Response(JSON.stringify(next || {}), { status: 200 });
    }
    if (peerAnswer) {
        const a = typeof peerAnswer === 'function' ? peerAnswer(url) : peerAnswer;
        if (a.hook) a.hook();
        if (a.delay) {
            peerInflight += 1;
            peerInflightMax = Math.max(peerInflightMax, peerInflight);
            await new Promise(r => setTimeout(r, a.delay));
            peerInflight -= 1;
        }
        if (a.throws) throw new TypeError('Failed to fetch');
        if (a.stall) {
            // The headers arrive; the body never does.
            return { status: a.status, ok: a.status >= 200 && a.status < 300,
                     headers: new Headers(a.headers || {}),
                     json: () => new Promise(() => {}) };
        }
        if (a.bodyDelay) {
            // The headers arrive at once; the body after bodyDelay ms.
            return { status: a.status, ok: a.status >= 200 && a.status < 300,
                     headers: new Headers(a.headers || {}),
                     json: () => new Promise(r => setTimeout(
                         () => r(a.body), a.bodyDelay)) };
        }
        return new Response(JSON.stringify(a.body), {
            status: a.status, headers: a.headers || {} });
    }
    return new Response('[]', { status: 200 });
}
"""


def _run(tmp_path, prefs, body, extra_src="", hosts_src=None):
    script = (PRELUDE + "let prefs = " + json.dumps(prefs) + ";\n"
              + (_hosts_source() if hosts_src is None else hosts_src)
              + _doors_source() + extra_src
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


@pytest.mark.parametrize("meanwhile", ["another candidate", "no socket in charge"])
def test_a_late_candidate_mint_dials_nothing(tmp_path, meanwhile):
    # Review round 1b SHOULD 5: a replacement's ticket arrives after another
    # replacement took its place, or after the socket in charge is gone.
    out = _dial(tmp_path, """
const host = hostById('rb');
const rec = { hostId: 'rb', ws: null, cur: { ws: null }, cand: null,
              closedByUs: false, attempts: 0 };
controlSockets.set('rb', rec);
const st = _dialControl(rec, host, 'cand');
const minting = !!rec.minting;
if (MEANWHILE === 'another candidate') rec.cand = { other: true };
else rec.cur = null;
await settle();
return { st: st, minting: minting, made: wsMade,
         cand: rec.cand && rec.cand.other === true, reattached: reattached };
""".replace("MEANWHILE", json.dumps(meanwhile)), OK_TICKET)
    assert out["st"] is None and out["minting"] is True
    assert out["made"] == 0
    assert out["reattached"] == []
    if meanwhile == "another candidate":
        assert out["cand"] is True          # the other one is left alone


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


# ---- phase 3 ----------------------------------------------------------------

STATE_STUBS = r"""
const hostStateCache = new Map();
const hostSaveChains = new Map();
let _stateRev = 0;
const CLIENT_ID = 'cid';
const notices = [];
function showNotice(t) { notices.push(t); }
function normalizeSettings(s) { return s; }
function getSettings() { return {}; }
function getLayout() { return {}; }
"""


def _state_src():
    s53 = _src("53_js_remote_host_cache.js")
    return (STATE_STUBS + _func(s53, "fetchHostState")
            + _func(s53, "putHostState") + _func(s53, "_putHostStateOnce"))


def test_a_proxied_hosts_state_is_never_fetched_or_put(tmp_path):
    """P3-5 (A1): /state is never relayed, so a proxied host's settings cache
    neither GETs nor PUTs it -- not even with the entry a settings tab
    captured -- while a direct host still does both."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
const entry = () => ({ rev: 1, settings: {}, layout: {}, layoutLoaded: true });
const g1 = await fetchHostState('rb');
const p1 = await putHostState('rb', entry());
const via = calls.length;
const g2 = await fetchHostState('rd');
const p2 = await putHostState('rd', entry());
return { g1: g1, p1: p1, via: via, g2: !!g2, p2: p2,
         direct: calls.map(c => [c.method || 'GET', c.url]) };
""".replace("INFO", json.dumps(INFO)), extra_src=_state_src())
    assert out["g1"] is None and out["p1"] == "skipped"
    assert out["via"] == 0
    assert out["g2"] is True and out["p2"] == "ok"
    assert out["direct"] == [["GET", "http://d:4445/state"],
                             ["PUT", "http://d:4445/state"]]


def test_a_proxied_hosts_settings_tab_says_why(tmp_path):
    s81 = _src("81_js_control_panel.js")
    out = _run(tmp_path, _prefs(), """
return [hostSettingsLoadError('rb'), hostSettingsLoadError('rd')];
""", extra_src=_func(s81, "hostSettingsLoadError"))
    assert out == ["settings are not available for a host reached through "
                   "this broker", "could not load this broker’s settings"]


def test_a_proxied_hosts_settings_tab_wording_is_used():
    """The call site (non-behavioural pin): the failed-load branch of
    selectSettingsTab shows hostSettingsLoadError's text."""
    tab = _func(_src("81_js_control_panel.js"), "selectSettingsTab")
    a = tab.index("if (!entry) {")
    assert ("setHostLoadingEl.textContent = hostSettingsLoadError(tabId);"
            in tab[a:a + 400])


DUP_STUBS = r"""
function normalizeHostUrl(u) { return String(u || '').replace(/\/+$/, ''); }
"""


def _dups(tmp_path, hosts):
    s83 = _src("83_js_broker_identity.js")
    out = _run(tmp_path, {"_hosts": hosts}, """
const m = computeHostDuplicates(getHosts());
const o = {};
for (const [k, v] of m) o[k] = v.id;
return o;
""", extra_src=DUP_STUBS + _func(s83, "hostDupKey")
        + _func(s83, "computeHostDuplicates"))
    return out


def _via(id_, peer="b", **kw):
    h = {"id": id_, "label": id_, "url": "http://b:4448", "token": "",
         "via": "server", "peerId": peer, "peerOrigin": "http://b:4448"}
    h.update(kw)
    return h


def _direct(id_, url="http://b:4448", **kw):
    h = {"id": id_, "label": id_, "url": url, "token": ""}
    h.update(kw)
    return h


def test_a_direct_and_a_proxied_record_of_one_broker_are_not_twins(tmp_path):
    """P3-4 (A2): the duplicate check is per mode, keyed on the peer id for a
    proxied record."""
    local = {"id": "local"}
    # same url, same learned broker_id, two modes -> nothing flagged
    assert _dups(tmp_path, [local, _direct("d1", brokerId="B-ID"),
                            _via("p1", brokerId="B-ID")]) == {}
    # ...and without broker ids, the url alone does not join them either
    assert _dups(tmp_path, [local, _direct("d1"), _via("p1")]) == {}
    # two proxied records naming one peer ARE twins
    assert _dups(tmp_path, [local, _via("p1"), _via("p2")]) == {"p2": "p1"}
    # ...two naming different peers are not, whatever url they display
    assert _dups(tmp_path, [local, _via("p1"), _via("p2", peer="c")]) == {}
    # two proxied records of one broker under two peer ids, ids learned
    assert _dups(tmp_path, [local, _via("p1", brokerId="B-ID"),
                            _via("p2", peer="c", brokerId="B-ID")]) \
        == {"p2": "p1"}
    # unchanged: two direct records at one url, and one pointing back here
    assert _dups(tmp_path, [local, _direct("d1"), _direct("d2")]) \
        == {"d2": "d1"}
    assert _dups(tmp_path, [local, _direct("d1", url="http://a:4445")]) \
        == {"d1": "local"}


OSC_STUBS = r"""
const store = new Map();
const localStorage = {
    getItem: (k) => store.has(k) ? store.get(k) : null,
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
};
const OSC_KEY = 'webterm:osc52Hosts:v1';
const stored = () => Object.keys(JSON.parse(store.get(OSC_KEY) || '{}'));
"""


def _osc_src():
    s63 = _src("63_js_clipboard_auth.js")
    return OSC_STUBS + s63[s63.index("// ---- OSC 52:"):
                           s63.index("// ---- auth ---")]


def test_the_osc52_key_carries_the_pairing(tmp_path):
    """P3-3 (A3, D10): a proxied host's grant is keyed on its peer id and
    paired origin, so a change of either drops it; a direct host's key is
    today's, so a grant stored before this change still holds."""
    prefs = _prefs()
    prefs["_hosts"][2]["url"] = "http://d:4445"
    out = _run(tmp_path, prefs, """
// (a) a grant stored under today's literal key
store.set(OSC_KEY, JSON.stringify({ '["rd","http://d:4445"]': true }));
const a = isOsc52Allowed('rd');
// (b) a proxied grant, then a re-pairing
setOsc52Allowed('rb', true);
const b0 = isOsc52Allowed('rb');
const keyB = stored().filter(k => k.indexOf('"rb"') !== -1);
hostById('rb').peerOrigin = 'http://b2:4448';
const b1 = isOsc52Allowed('rb');
const b1keys = stored().filter(k => k.indexOf('"rb"') !== -1);
// (c) the same for a new peer id
hostById('rb').peerOrigin = 'http://b:4448';
setOsc52Allowed('rb', true);
hostById('rb').peerId = 'c';
const c1 = isOsc52Allowed('rb');
// (d) direct -> proxied
setOsc52Allowed('rd', true);
const d0 = isOsc52Allowed('rd');
Object.assign(hostById('rd'), { via: 'server', peerId: 'd',
                                peerOrigin: 'http://d:4445' });
const d1 = isOsc52Allowed('rd');
return { a: a, b0: b0, keyB: keyB, b1: b1, b1keys: b1keys, c1: c1,
         d0: d0, d1: d1 };
""", extra_src=_osc_src())
    assert out["a"] is True
    assert out["b0"] is True
    assert out["keyB"] == ['["rb","http://b:4448","b","http://b:4448"]']
    assert out["b1"] is False and out["b1keys"] == []
    assert out["c1"] is False
    assert out["d0"] is True and out["d1"] is False


MARKED_401 = {"status": 401, "body": {"ok": False, "error": "auth_required"},
              "headers": {"X-Browserland-Peer-Refusal": "auth_required"}}
PEERS_401 = {"status": 401, "body": {"ok": False, "error": "auth_required"}}


def _attrib(tmp_path, body, answer, token=B_TOKEN, info=INFO):
    pre = ("infoAnswers = [INFO, INFO, INFO];\n"
           .replace("INFO", json.dumps(info))
           + "peerAnswer = " + json.dumps(answer) + ";\n")
    s75 = _src("75_js_taskbar_hosts.js")
    return _run(tmp_path, _prefs(token=token), pre + body,
                extra_src=_dial_src() + _func(s75, "pollHost"))


ATTRIB_BODY = """
const r = await hostFetch(hostById('rb'), '/sessions');
let j = null; try { j = await r.json(); } catch (_) {}
return { status: r.status, error: j && j.error, overlays: overlays,
         localAuth: pollStateFor('local').authNeeded,
         bAuth: pollStateFor('rb').authNeeded, token: hostById('rb').token };
"""


def test_this_brokers_own_401_opens_this_brokers_overlay(tmp_path):
    """P3-2 (A4): a 401 with this broker's marker on a request that SENT a
    peer token is this broker's bearer rung: its own overlay opens, and the
    caller sees a 503 hub_auth_required -- never the peer's login."""
    out = _attrib(tmp_path, ATTRIB_BODY, MARKED_401)
    assert out["status"] == 503 and out["error"] == "hub_auth_required"
    assert out["overlays"] == ["local"]
    assert out["localAuth"] is True and out["bAuth"] is False
    assert out["token"] == B_TOKEN


def test_the_peers_own_401_still_reaches_the_caller(tmp_path):
    out = _attrib(tmp_path, ATTRIB_BODY, PEERS_401)
    assert out["status"] == 401 and out["overlays"] == []
    assert out["localAuth"] is False


def test_a_marked_401_without_a_peer_token_stays_the_peers(tmp_path):
    """No peer token sent: the 401 may be either rung, and the peer needs its
    password regardless -- it passes as a 401 (the peer's login)."""
    out = _attrib(tmp_path, ATTRIB_BODY, MARKED_401, token="")
    assert out["status"] == 401 and out["overlays"] == []


def test_both_polls_flag_auth_when_neither_token_is_known(tmp_path):
    """S4: a wrong token here and no peer token: this broker's OWN poll still
    flags it (its /sessions answers 401 too); which overlay the user sees
    first is the order the two answers arrive (documented)."""
    out = _attrib(tmp_path, """
await pollHost(localHost());
await pollHost(hostById('rb'));
return { overlays: overlays, localAuth: pollStateFor('local').authNeeded,
         bAuth: pollStateFor('rb').authNeeded };
""", MARKED_401, token="")
    assert out["localAuth"] is True and out["bAuth"] is True
    assert out["overlays"] == ["local", "rb"]


def test_a_wrong_token_before_info_was_read_is_a_down_host(tmp_path):
    """S3: with no /info memo, a wrong token here rejects at /info -- the
    caller reads it as down, and nobody's overlay opens from _peerFetch."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [401];
let threw = null;
try { await hostFetch(hostById('rb'), '/sessions'); }
catch (e) { threw = String(e); }
return { threw: threw, overlays: overlays,
         calls: calls.map(c => c.url) };
""", extra_src=_dial_src())
    assert "/info: HTTP 401" in out["threw"]
    assert out["overlays"] == []
    assert out["calls"] == ["/info"]


def test_a_mint_refused_by_this_brokers_gate_names_it(tmp_path):
    out = _attrib(tmp_path, """
let threw = null;
try { await hostWsUrl(hostById('rb'), '/ws?session=1'); }
catch (e) { threw = String(e); }
return { threw: threw, overlays: overlays,
         refusal: pollStateFor('rb').wsRefusal,
         tip: hostChipTip(hostById('rb'), 'ok') };
""", MARKED_401)
    assert "hub_auth_required" in out["threw"]
    assert out["overlays"] == ["local"]
    assert out["refusal"] == "hub_auth_required"
    assert "terminals: this broker needs its own password" in out["tip"]


LOGIN_STUBS = r"""
let authOverlayHostId = null;
let authSubmitting = false;
let authGeneration = 0;
const authTokenEl = { value: '' };
const authSubmitBtn = { disabled: false, textContent: 'log in' };
const authErrorEl = { textContent: '', classList: { add() {} } };
const authPrompted = new Set();
const hostPolls = new Map();
const windows = new Map();
function hideAuthOverlay() { overlays.push('hide'); }
let _submit = null;
const document = { getElementById: (id) => id === 'auth-form'
    ? { addEventListener: (t, fn) => { _submit = fn; } } : null };
"""


def _login_src():
    s63 = _src("63_js_clipboard_auth.js")
    a = s63.index("        document.getElementById('auth-form')"
                  ".addEventListener('submit', async (e) => {")
    b = s63.index("\n        });\n", a) + len("\n        });\n")
    return LOGIN_STUBS + s63[a:b]


def test_the_login_probe_says_when_this_broker_refused_it(tmp_path):
    """A4: a proxied host's login probe refused by this broker's own gate says
    so, and stores nothing (the password typed may be right)."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
peerAnswer = MARKED;
authOverlayHostId = 'rb';
authTokenEl.value = 'NEW-B';
await _submit({ preventDefault() {} });
return { msg: authErrorEl.textContent, token: hostById('rb').token };
""".replace("INFO", json.dumps(INFO)).replace("MARKED", json.dumps(MARKED_401)),
        extra_src=_dial_src() + _login_src())
    assert out["msg"] == "this broker needs its own password"
    assert out["token"] == B_TOKEN


DOM_STUBS = r"""
function el() {
    const e = { children: [], textContent: '', title: '', className: '',
                attrs: {}, classList: { _s: new Set(),
                    add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); } },
                appendChild(c) { this.children.push(c); return c; },
                setAttribute(k, v) { this.attrs[k] = v; },
                addEventListener() {}, focus() {} };
    return e;
}
const document = { createElement: () => el() };
const authOverlay = el(), authErrorEl = el(), authTokenEl = el();
const authHostLine = el();
let authOverlayHostId = null, authGeneration = 0;
const authPrompted = new Set();
function openLaunchMenu() {}
"""


def _identity_src():
    s63 = _src("63_js_clipboard_auth.js")
    s75 = _src("75_js_taskbar_hosts.js")
    return (_dial_src() + DOM_STUBS + _func(s63, "showAuthOverlay")
            + _func(s75, "hostStateSuffix")
            + _func(s75, "renderAggregateChip"))


def test_a_proxied_host_says_via_this_broker(tmp_path):
    """P3-2 (A5): same host id, same label; the overlay line, the chip tip and
    the aggregate chip's per-host line add "(via this broker)". A direct
    host's text is byte-identical to before."""
    prefs = _prefs()
    prefs["_hosts"][2]["url"] = "http://d:4445"
    out = _run(tmp_path, prefs, """
const lines = {};
for (const id of ['rb', 'rd']) {
    authOverlay.classList.remove('open');
    showAuthOverlay(hostById(id), true);
    lines[id] = authHostLine.textContent;
}
const box = el();
const hosts = getHosts();
renderAggregateChip(box, hosts, hosts.map(() => 'ok'));
return { overlay: lines, tipB: hostChipTip(hostById('rb'), 'ok'),
         tipD: hostChipTip(hostById('rd'), 'ok'),
         agg: box.children[0].title };
""", extra_src=_identity_src())
    assert out["overlay"] == {"rb": "B (http://b:4448) (via this broker)",
                              "rd": "D (http://d:4445)"}
    assert out["tipB"] == "http://b:4448 (via this broker) — click to hide"
    assert out["tipD"] == "http://d:4445 — click to hide"
    assert out["agg"].split("\n")[1:3] == ["B (via this broker) — ok",
                                          "D — ok"]


def test_the_hosts_row_names_the_mode():
    """The Hosts row's name (non-behavioural call-site pin; the Playwright
    pass shows it)."""
    rows = _func(_src("83_js_broker_identity.js"), "renderHostsList")
    assert ": host.label + ' — ' + host.url + hostViaNote(host);" in rows


def test_the_info_memo_is_read_again_after_this_broker_restarts(tmp_path):
    """P3-6 (A6): the localPeers() memo survives the same bootId and drops on
    a new one; the /info answer seeds the id it came from, and a missing id
    (an older build) never forgets."""
    info0 = dict(INFO, restart={"bootId": "b0"})
    out = _run(tmp_path, _prefs(), """
const n = () => calls.filter(c => c.url === '/info').length;
infoAnswers = [I0, INFO, INFO, INFO];
await localPeers(); await localPeers();
const once = n();
noteLocalBootId('b0'); await localPeers();
const sameAsSeed = n();
noteLocalBootId(null); noteLocalBootId(''); noteLocalBootId(undefined);
await localPeers();
const missing = n();
noteLocalBootId('b1'); await localPeers();
const restarted = n();
noteLocalBootId('b1'); await localPeers();
const steady = n();
return { once: once, sameAsSeed: sameAsSeed, missing: missing,
         restarted: restarted, steady: steady };
""".replace("I0", json.dumps(info0)).replace("INFO", json.dumps(INFO)))
    assert out == {"once": 1, "sameAsSeed": 1, "missing": 1,
                   "restarted": 2, "steady": 2}


def test_without_a_seed_the_first_bootid_only_seeds(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
await localPeers();
noteLocalBootId('x'); await localPeers();
const a = calls.length;
noteLocalBootId('y'); await localPeers();
return [a, calls.length];
""".replace("INFO", json.dumps(INFO)))
    assert out == [1, 2]


def test_the_info_answer_seeds_the_bootid(tmp_path):
    """The memo read under run b0, then a control status from run b1 (this
    broker restarted between the two): the memo is stale, read again."""
    info0 = dict(INFO, restart={"bootId": "b0"})
    out = _run(tmp_path, _prefs(), """
infoAnswers = [I0, INFO];
await localPeers();
noteLocalBootId('b1'); await localPeers();
return calls.filter(c => c.url === '/info').length;
""".replace("I0", json.dumps(info0)).replace("INFO", json.dumps(INFO)))
    assert out == 2


MODE_STUBS = DUP_STUBS + r"""
const emitted = [];
function _emitModEvent(n, p) { emitted.push([n, p.hostId]); }
"""


def _mode(tmp_path, hosts, body, info=INFO):
    return _run(tmp_path, {"_hosts": [{"id": "local", "token": A_TOKEN}]
                           + hosts},
                # localPeers()'s shape, which setHostMode takes
                "const info = " + json.dumps({"proxy": info["peer_proxy"],
                                              "peers": info["peers"]})
                + ";\n" + body,
                extra_src=MODE_STUBS + _osc_src())


#: A dotted DNS name: the only kind of origin a switch keeps a token for
#: (round 1b S3).
DOTTED = "https://b.example.com"
DOTTED_INFO = {"ok": True, "peers": {"b": DOTTED, "c": DOTTED, "e": DOTTED},
               "peer_proxy": True}


SNAP = """
const snap = (id) => { const h = hostById(id); return h && {
    url: h.url, token: h.token, via: h.via || null, peerId: h.peerId || null,
    peerOrigin: h.peerOrigin === undefined ? null : h.peerOrigin,
    brokerId: h.brokerId === undefined ? null : h.brokerId }; };
"""


def test_switching_a_host_to_from_server_and_back(tmp_path):
    """P3-1 (A7): the token survives a switch only where it was paired with
    the address now dialed; caches are invalidated, the identity is dropped,
    the change is announced."""
    # round 1b S3: a token is kept only for a dotted DNS name
    out = _mode(tmp_path, [
        _direct("same", url=DOTTED, token="T1", brokerId="B-ID"),
        _direct("other", url="http://d:4445", token="T2"),
        _via("paired", peer="c", url=DOTTED, peerOrigin=DOTTED, token="T3"),
        _via("unpaired", peer="c", url="https://u.example.com", token="T4",
             peerOrigin=""),
    ], SNAP + """
const r = {};
r.same = [setHostMode('same', 'b', info), snap('same')];
r.other = [setHostMode('other', 'e', info), snap('other')];
r.paired = [setHostMode('paired', null, info), snap('paired')];
r.unpaired = [setHostMode('unpaired', null, info), snap('unpaired')];
r.local = setHostMode('local', 'b', info);
r.unknownPeer = setHostMode('other', 'zz', info);
r.samePeer = setHostMode('other', 'e', info);
r.unknownId = setHostMode('nope', null, info);
return { r: r, invalidated: invalidated, emitted: emitted,
         saves: saves.length };
""", info=DOTTED_INFO)
    r = out["r"]
    assert r["same"] == [True, {"url": DOTTED, "token": "T1",
                                "via": "server", "peerId": "b",
                                "peerOrigin": DOTTED,
                                "brokerId": ""}]
    assert r["other"] == [True, {"url": DOTTED, "token": "",
                                 "via": "server", "peerId": "e",
                                 "peerOrigin": DOTTED,
                                 "brokerId": ""}]
    assert r["paired"] == [True, {"url": DOTTED, "token": "T3",
                                  "via": None, "peerId": None,
                                  "peerOrigin": None, "brokerId": ""}]
    assert r["unpaired"][1]["token"] == "" and r["unpaired"][1]["via"] is None
    assert [r["local"], r["unknownPeer"], r["samePeer"], r["unknownId"]] \
        == [False, False, False, False]
    assert out["invalidated"] == ["same", "other", "paired", "unpaired"]
    assert out["emitted"] == [["host:changed", h] for h in
                              ("same", "other", "paired", "unpaired")]
    assert out["saves"] == 4


def test_a_host_on_a_broker_that_cannot_proxy_can_still_go_direct(tmp_path):
    out = _mode(tmp_path, [_direct("d", token="T"),
                           _via("p", url=DOTTED, peerOrigin=DOTTED,
                                token="T3")],
                SNAP + """
return [setHostMode('d', 'b', info), snap('d').via,
        setHostMode('p', null, info), snap('p').via, snap('p').token];
""", info={"ok": True, "peers": {"b": DOTTED}, "peer_proxy": False})
    assert out == [False, None, True, None, "T3"]


def test_a_proxied_host_moved_to_another_peer_drops_its_token(tmp_path):
    # a dotted origin, so only "it was paired with another peer" drops it
    info = dict(INFO, peers={"b": DOTTED, "c": DOTTED})
    out = _mode(tmp_path, [_via("p", url=DOTTED, peerOrigin=DOTTED,
                                token="T3")], SNAP + """
return [setHostMode('p', 'c', info), snap('p')];
""", info=info)
    assert out[0] is True
    assert out[1]["token"] == "" and out[1]["peerId"] == "c"


@pytest.mark.parametrize("origin", [
    "http://localhost:4448", "http://localhost.:4448", "http://127.0.0.1:4448",
    "http://127.1:4448", "http://127.9.9.9:4448", "http://[::1]:4448",
    "http://0.0.0.0:4448", "http://[::]:4448", "http://app.localhost:4448",
    "http://[::ffff:127.0.0.1]:4448", "http://[::ffff:0.0.0.0]:4448",
    # round 1b S3: every IP literal, every one-word name, local suffixes
    "http://ip6-localhost:4448", "http://ip6-loopback:4448",
    "http://[::7f00:1]:4448", "http://192.168.1.5:4448", "http://10.0.0.5:4448",
    "http://100.64.0.2:4448", "http://[fe80::1]:4448", "http://nas:4448",
    "http://b.lan:4448", "http://b.local:4448", "http://b.internal:4448",
    "http://b.home.arpa:4448", "http://b.localdomain:4448"])
def test_an_ambiguous_origin_never_keeps_a_token(tmp_path, origin):
    """S1 / round 1b S3: the browser and this broker resolve a name on their
    own networks, so only a dotted DNS name outside the local-only suffixes
    is taken to mean one machine to both. Everything else asks again, in
    both directions."""
    info = dict(INFO, peers={"l": origin, "m": origin})
    out = _mode(tmp_path, [
        _direct("d", url=origin, token="T1"),
        _via("p", peer="m", url=origin, peerOrigin=origin, token="T2"),
    ], SNAP + """
return [setHostMode('d', 'l', info), snap('d').token,
        setHostMode('p', null, info), snap('p').token];
""", info=info)
    assert out == [True, "", True, ""]


@pytest.mark.parametrize("origin", ["https://b.example.com",
                                    "http://b.example.com:4448",
                                    "https://box.tailnet-x.ts.net"])
def test_a_dotted_dns_name_keeps_its_token_both_ways(tmp_path, origin):
    info = dict(INFO, peers={"l": origin, "m": origin})
    out = _mode(tmp_path, [
        _direct("d", url=origin, token="T1"),
        _via("p", peer="m", url=origin, peerOrigin=origin, token="T2"),
    ], SNAP + """
return [originKeepsToken(info.peers.l), setHostMode('d', 'l', info),
        snap('d').token, setHostMode('p', null, info), snap('p').token];
""", info=info)
    assert out == [True, True, "T1", True, "T2"]


def test_a_switch_and_back_does_not_revive_an_osc52_grant(tmp_path):
    """S2: grant on direct, switch to from server and back with no OSC 52
    check in between: the url is the same again, so only the prune run by the
    switch keeps the old grant from re-arming."""
    out = _mode(tmp_path, [_direct("d", token="T1")], """
setOsc52Allowed('d', true);
const before = isOsc52Allowed('d');
setHostMode('d', 'b', info);
const mid = stored().length;
setHostMode('d', null, info);
return [before, mid, isOsc52Allowed('d')];
""")
    assert out == [True, 0, False]


def _opts(tmp_path, hosts, info):
    return _run(tmp_path, {"_hosts": [{"id": "local"}] + hosts}, """
const info = INFO;
const out = {};
for (const h of getHosts()) out[h.id] = hostModeOptions(h, info);
return out;
""".replace("INFO", json.dumps(info)), extra_src=DUP_STUBS)


def test_the_mode_control_offers_what_this_broker_publishes(tmp_path):
    """P3-1 (A8): direct + one row per published peer, the one at the host's
    own address first; nothing while this broker cannot proxy (a direct
    host); a proxied host always keeps its way back to direct."""
    info = {"proxy": True, "peers": {"a": "http://x:1", "b": "http://b:4448"}}
    out = _opts(tmp_path, [_direct("d"), _via("p"), _via("gone", peer="z")],
                info)
    assert out["local"] == []
    assert [(o["value"], o["label"], o["selected"]) for o in out["d"]] == [
        ("", "direct", True),
        ("b", "from server · b (same address)", False),
        ("a", "from server · a", False)]
    assert [(o["value"], o["selected"]) for o in out["p"]] == [
        ("", False), ("b", True), ("a", False)]
    assert out["gone"][-1] == {"value": "z", "disabled": True,
                               "selected": True,
                               "label": "from server · z (unavailable)"}
    off = _opts(tmp_path, [_direct("d"), _via("p")],
                {"proxy": False, "peers": {"b": "http://b:4448"}})
    assert off["d"] == []
    assert [(o["value"], o.get("disabled", False)) for o in off["p"]] == [
        ("", False), ("b", True)]
    none = _opts(tmp_path, [_direct("d")], {"proxy": True, "peers": {}})
    assert none["d"] == []


def test_the_mode_refresh_re_renders_only_when_the_answer_moved(tmp_path):
    """S6(a): first answer -> one render; the same answer -> none; a failed
    /info -> none (and no second ask: nothing loops)."""
    out = _run(tmp_path, _prefs(), """
let renders = 0;
const onChange = () => { renders++; };
infoAnswers = [INFO];
await refreshHostModeInfo(onChange);
const first = renders;
await refreshHostModeInfo(onChange);
const same = renders;
forgetLocalPeers();
infoAnswers = ['reject'];
await refreshHostModeInfo(onChange);
await new Promise(r => setTimeout(r, 20));
return { first: first, same: same, failed: renders,
         infos: calls.filter(c => c.url === '/info').length };
""".replace("INFO", json.dumps(INFO)))
    assert out == {"first": 1, "same": 1, "failed": 1, "infos": 2}


REHOME_STUBS = DUP_STUBS + r"""
const windows = new Map();
const redialed = [];
const controlOpened = [];
let hostListRenders = 0;
function reattachWindow(w) { redialed.push(w.key); }
function openControlWs(h) { controlOpened.push(h.id); }
function renderHostsList() { hostListRenders++; }
function _emitModEvent() {}
const hostErrors = [];
function setHostError(m) { hostErrors.push(m); }
"""


def test_a_switch_re_dials_that_hosts_sockets_and_no_others(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO];
windows.set('w1', { key: 'w1', hostId: 'rd', reattachAttempts: 3 });
windows.set('w2', { key: 'w2', hostId: 'rd' });
windows.set('w3', { key: 'w3', hostId: 'rb' });
windows.set('w4', { key: 'w4', hostId: 'rd', disposed: true });
const changed = await switchHostMode('rd', 'c');
return { changed: changed, redialed: redialed, control: controlOpened,
         attempts: windows.get('w1').reattachAttempts,
         renders: hostListRenders, via: hostById('rd').via };
""".replace("INFO", json.dumps(dict(INFO, peers={"b": "http://b:4448",
                                                 "c": "http://c:4448"}))),
               extra_src=REHOME_STUBS + _osc_src())
    assert out == {"changed": True, "redialed": ["w1", "w2"],
                   "control": ["rd"], "attempts": 0, "renders": 1,
                   "via": "server"}


def test_a_switch_to_direct_works_when_info_cannot_be_read(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = ['reject'];
windows.set('w3', { key: 'w3', hostId: 'rb' });
const changed = await switchHostMode('rb', null);
const toServer = await switchHostMode('rd', 'b');
return { changed: changed, toServer: toServer, redialed: redialed,
         via: hostById('rb').via || null, renders: hostListRenders };
""", extra_src=REHOME_STUBS + _osc_src())
    assert out == {"changed": True, "toServer": False, "redialed": ["w3"],
                   "via": None, "renders": 2}


def _load_src():
    s56 = _src("56_js_hosts.js")
    a = s56.index("        function getHosts() {")
    b = s56.index("        // #253: does this host's broker host NO terminals")
    return ("const document = { cookie: '' };\n" + DUP_STUBS + s56[a:b])


def _run_load(tmp_path, prefs, body):
    """Like _run, but with the load-time block of 56 too (the CSP cookie, the
    two load snapshots and their predicates) in place of the hosts slice."""
    return _run(tmp_path, prefs, body, hosts_src=_load_src())


def test_a_host_switched_to_direct_after_load_says_so(tmp_path):
    """S6(c) / Q2: a host proxied at load and direct now was never in this
    page's CSP origins -- its own note (not "added"); a host direct at load,
    or one added after load, does not get it."""
    prefs = _prefs()
    prefs["_hosts"].append(_direct("bounce", url="http://e:4445"))
    out = _run_load(tmp_path, prefs, """
const rb = hostById('rb');
const rbVia = hostSwitchedAfterLoad(rb);
delete rb.via; delete rb.peerId; delete rb.peerOrigin;
const bounce = hostById('bounce');
Object.assign(bounce, { via: 'server', peerId: 'e' });
const bounceVia = hostSwitchedAfterLoad(bounce);
delete bounce.via; delete bounce.peerId;
prefs._hosts.push({ id: 'fresh', label: 'f', url: 'http://f:1', token: '' });
return { rbVia: rbVia, rb: hostSwitchedAfterLoad(rb),
         rd: hostSwitchedAfterLoad(hostById('rd')),
         bounceVia: bounceVia, bounce: hostSwitchedAfterLoad(bounce),
         fresh: hostSwitchedAfterLoad(hostById('fresh')),
         freshAdded: hostAddedAfterLoad('fresh'),
         local: hostSwitchedAfterLoad(localHost()), note: HOST_SWITCHED_NOTE };
""")
    assert out == {"rbVia": False, "rb": True, "rd": False,
                   "bounceVia": False,
                   "bounce": False, "fresh": False, "freshAdded": True,
                   "local": False,
                   "note": "direct now — reconnects directly; if it does "
                           "not, reload the page"}


def test_the_hosts_row_wires_the_mode_control_and_the_note():
    """The DOM wiring (non-behavioural pins; the Playwright pass drives it)."""
    rows = _func(_src("83_js_broker_identity.js"), "renderHostsList")
    assert "const modes = hostModeOptions(host, localPeersKnown());" in rows
    assert "() => switchHostMode(host.id, sel.value || null));" in rows
    assert "} else if (hostSwitchedAfterLoad(host)) {" in rows
    assert "moved.textContent = HOST_SWITCHED_NOTE;" in rows
    assert "refreshHostModeInfo(renderHostsList);" in rows


GEN_BODY = """
let n = 0;
const answers = ANSWERS;
peerAnswer = (url) => answers[n++];
const p1 = hostWsUrl(hostById('rb'), '/ws?session=1').catch(e => String(e));
await new Promise(r => setTimeout(r, 5));
const p2 = hostWsUrl(hostById('rb'), '/ws?session=2').catch(e => String(e));
const r = await Promise.all([p1, p2]);
await settle();
return { results: r, refusal: pollStateFor('rb').wsRefusal || null };
"""
SLOW_BUSY = dict(BUSY, delay=60)
SLOW_OK = dict(OK_TICKET, delay=60)


def test_an_older_mints_late_refusal_does_not_cover_a_newer_success(tmp_path):
    """Phase-3 atom 13: the chip shows the NEWEST mint's outcome per host."""
    out = _dial(tmp_path, GEN_BODY.replace(
        "ANSWERS", json.dumps([SLOW_BUSY, OK_TICKET])))
    assert "peer_busy" in out["results"][0]
    assert out["results"][1].endswith("ticket=T")
    assert out["refusal"] is None


def test_an_older_mints_late_success_does_not_clear_a_newer_refusal(tmp_path):
    out = _dial(tmp_path, GEN_BODY.replace(
        "ANSWERS", json.dumps([SLOW_OK, BUSY])))
    assert out["results"][0].endswith("ticket=T")
    assert "peer_busy" in out["results"][1]
    assert out["refusal"] == "peer_busy"


def test_a_put_queued_while_direct_never_leaves_after_a_switch(tmp_path):
    """codex 2: putHostState serializes per host, so a save queued behind a
    slow one while the host was direct runs AFTER it was switched; the check
    sits on that path too."""
    out = _run(tmp_path, _prefs(), """
peerAnswer = { status: 200, body: { rev: 2 }, delay: 40 };
const entry = () => ({ rev: 1, settings: {}, layout: {}, layoutLoaded: true });
const p1 = putHostState('rd', entry());
const p2 = putHostState('rd', entry());
await new Promise(r => setTimeout(r, 10));
Object.assign(hostById('rd'), { via: 'server', peerId: 'b',
                                peerOrigin: 'http://b:4448' });
const r = await Promise.all([p1, p2]);
return { r: r, puts: calls.filter(c => c.method === 'PUT').map(c => c.url) };
""", extra_src=_state_src())
    assert out == {"r": ["ok", "skipped"], "puts": ["http://d:4445/state"]}


MOVED_BODY = """
peerAnswer = ANSWER;
const p = hostWsUrl(hostById('rb'), '/ws?session=1').catch(e => 'threw: ' + e);
await new Promise(r => setTimeout(r, 10));
MOVE
const out = await p;
await settle();
return { out: out, refusal: pollStateFor('rb').wsRefusal || null,
         overlays: overlays };
"""
TO_DIRECT = ("const h = hostById('rb'); delete h.via; delete h.peerId; "
             "delete h.peerOrigin;")
TO_C = "hostById('rb').peerId = 'c';"


@pytest.mark.parametrize("answer", [
    dict(BUSY, delay=60), dict(OK_TICKET, delay=60), dict(PEERS_401, delay=60),
    # moved while the body is still on its way
    dict(OK_TICKET, bodyDelay=60), dict(BUSY, bodyDelay=60),
    # moved, then the mint itself fails
    {"status": 0, "throws": True, "delay": 60}])
@pytest.mark.parametrize("move", [TO_DIRECT, TO_C])
def test_a_mint_that_settles_after_a_switch_touches_nothing(tmp_path, answer,
                                                            move):
    """codex 3/9: the record was switched to direct, or to another peer,
    while its ticket mint was out: the late answer paints no reason, opens
    no overlay and dials nothing (the switch re-dialed the windows itself)."""
    out = _dial(tmp_path, MOVED_BODY.replace("ANSWER", json.dumps(answer))
                .replace("MOVE", move))
    assert out == {"out": None, "refusal": None, "overlays": []}


def test_a_re_pairing_round_trip_does_not_revive_an_osc52_grant(tmp_path):
    """codex 4 (D10): this broker publishes the peer elsewhere, then at the
    old origin again, with no clipboard check in between: the grant made for
    the first pairing is gone, not re-armed."""
    out = _run(tmp_path, _prefs(), """
setOsc52Allowed('rb', true);
const before = isOsc52Allowed('rb');
rePairPeers({ proxy: true, peers: { b: 'http://b2:4448' } });
const mid = stored().length;
rePairPeers({ proxy: true, peers: { b: 'http://b:4448' } });
return [before, mid, isOsc52Allowed('rb')];
""", extra_src=_osc_src())
    assert out == [True, 0, False]


def test_a_pairing_409_prunes_the_grant_at_once(tmp_path):
    """The 409 path empties peerOrigin; if the re-read lands on the same
    origin again, the old grant must not match again."""
    out = _run(tmp_path, _prefs(), """
setOsc52Allowed('rb', true);
infoAnswers = [INFO, INFO];
peerAnswer = { status: 409, body: {},
               headers: { 'X-Browserland-Peer-Refusal': 'peer_origin_changed' } };
const r = await hostFetch(hostById('rb'), '/sessions');
return [r.status, hostById('rb').peerOrigin, isOsc52Allowed('rb')];
""".replace("INFO", json.dumps(INFO)), extra_src=_osc_src())
    assert out == [401, "http://b:4448", False]


def test_an_info_answer_that_lost_the_race_commits_nothing(tmp_path):
    """codex 6/10: this broker restarts while an /info read is out (its
    control status brings a new bootId, which forgets the memo) and the new
    run publishes the peer elsewhere. The newer read wins; the older answer,
    landing last, re-pairs nothing, paints nothing and seeds no bootId."""
    old = dict(INFO, peers={"b": "http://old:4448"}, restart={"bootId": "b0"},
               __delay=60)
    new = dict(INFO, restart={"bootId": "b1"})
    out = _run(tmp_path, _prefs(), """
infoAnswers = [OLD, NEW, NEW];
const p1 = localPeers();
await new Promise(r => setTimeout(r, 10));
noteLocalBootId('b0'); noteLocalBootId('b1');   // the restart, seen live
const p2 = localPeers();
await Promise.all([p1, p2]);
const rb = hostById('rb');
const known = localPeersKnown().peers.b;
const infosBefore = calls.filter(c => c.url === '/info').length;
noteLocalBootId('b1'); await localPeers();      // b1 is the seed: no re-read
return { token: rb.token, peerOrigin: rb.peerOrigin, known: known,
         reread: calls.filter(c => c.url === '/info').length - infosBefore };
""".replace("OLD", json.dumps(old)).replace("NEW", json.dumps(new)))
    assert out == {"token": B_TOKEN, "peerOrigin": "http://b:4448",
                   "known": "http://b:4448", "reread": 0}


def test_a_failed_info_read_forgets_the_painted_answer(tmp_path):
    """codex 5: after a failed read the Hosts row no longer offers the peers
    of an answer from before (without a render of its own -- S6)."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, 'reject'];
await localPeers();
const before = !!localPeersKnown();
forgetLocalPeers();
try { await localPeers(); } catch (_) {}
await new Promise(r => setTimeout(r, 10));
return [before, localPeersKnown()];
""".replace("INFO", json.dumps(INFO)))
    assert out == [True, None]


def _css_rule(css, selector):
    """The declarations of the one rule whose selector is exactly
    ``selector``, as {property: value}."""
    hits = [m for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
            if m.group(1).strip().split("\n")[-1].strip() == selector]
    assert len(hits) == 1, (selector, len(hits))
    out = {}
    for decl in hits[0].group(2).split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_the_host_notes_take_their_own_line_under_the_row():
    """Round 1 SHOULD 1: the notes under a Hosts row ("added — reload to
    connect", the 77-character "direct now — …") wrap as text on a full-width
    last line; without these rules the note shrank to a one-word column and
    the row widened the pane (measured live: 1181 px in a 589 px pane)."""
    css = _src("15_css_dialogs.css")
    row = _css_rule(css, ".host-row")
    assert row.get("flex-wrap") == "wrap"
    # Round 2 SHOULD 1: the name keeps a real basis, so it reads whole at
    # the default pane width (measured live: "this broker (127.0.0.1:4447)"
    # + its default badge and "B — http://127.0.0.1:4448" not ellipsised,
    # note 589 = row 589, no overflow); with a zero basis B's name was a
    # 3 px stub. min-width 0 still lets a name wider than the pane ellipsise.
    name = _css_rule(css, ".host-row .host-name")
    assert name.get("flex") == "1 1 auto"
    assert name.get("min-width") == "0"
    note = _css_rule(css, ".host-row .host-added-note")
    assert note.get("flex") == "1 0 100%"
    assert note.get("order") == "1"
    assert note.get("white-space") == "normal"


TWIN_BODY = SNAP + """
const before = JSON.stringify(getHosts());
const r = setHostMode(ID, PEER, info);
const unchanged = JSON.stringify(getHosts()) === before;
const ui = await switchHostMode(ID, PEER);
return { r: r, unchanged: unchanged, ui: ui, errors: hostErrors,
         saves: saves.length, invalidated: invalidated,
         redialed: redialed, control: controlOpened,
         renders: hostListRenders };
"""


@pytest.mark.parametrize("hosts, id_, peer", [
    # a proxied record switched to direct next to a direct one at its url
    ([("direct", "d"), ("via", "p")], "p", None),
    # a direct record switched to a peer another record already uses
    ([("via", "p"), ("direct", "q", "http://q:4445")], "q", "b"),
])
def test_a_switch_that_would_make_a_twin_is_refused(tmp_path, hosts, id_,
                                                    peer):
    """Round 1b S2: the add/edit forms refuse a second direct record at one
    url and a second record for one peer; so does the switch -- the same
    words, nothing written, nothing re-dialed."""
    recs = []
    for h in hosts:
        if h[0] == "direct":
            recs.append(_direct(h[1], url=(h[2] if len(h) > 2 else DOTTED),
                                token="T"))
        else:
            recs.append(_via(h[1], url=DOTTED, peerOrigin=DOTTED, token="T"))
    out = _run(tmp_path, {"_hosts": [{"id": "local", "token": A_TOKEN}]
                          + recs},
               "infoAnswers = [" + json.dumps(DOTTED_INFO) + "];\n"
               "const info = { proxy: true, peers: { b: '" + DOTTED + "' } };\n"
               + TWIN_BODY.replace("ID", json.dumps(id_))
               .replace("PEER", json.dumps(peer)),
               extra_src=REHOME_STUBS + _osc_src())
    assert out == {"r": False, "unchanged": True, "ui": False,
                   "errors": ["host already configured"], "saves": 0,
                   "invalidated": [], "redialed": [], "control": [],
                   "renders": 1}



FORM_STUBS = r"""
let editingHostId = null;
const hostLabelEl = { value: '', focus() {} };
const hostUrlEl = { value: '', disabled: false };
const hostPassEl = { value: '' };
const hostAddBtn = { textContent: 'add' };
function setHostError() {}
"""


def test_resetting_the_host_form_re_enables_the_url_box(tmp_path):
    """Round 1b S4: editing a proxied row disables the URL box (its address
    is this broker's config); leaving that edit -- save, cancel, or a tab
    switch, all through resetHostForm -- must enable it again, or no host
    can be added until a reload."""
    s82 = _src("82_js_settings_keys_hosts.js")
    s83 = _src("83_js_broker_identity.js")
    out = _run(tmp_path, _prefs(), """
startEditHost('rb');
const editingVia = hostUrlEl.disabled;
resetHostForm();
const afterReset = hostUrlEl.disabled;
startEditHost('rd');
return [editingVia, afterReset, hostUrlEl.disabled];
""", extra_src=FORM_STUBS + _func(s82, "resetHostForm")
        + _func(s83, "startEditHost"))
    assert out == [True, False, False]



def _mod_sync_fn(name):
    src = (BROKER / "mods" / "mod-sync" / "mod-sync.js").read_text(
        encoding="utf-8")
    a = src.index("                async function " + name + "(")
    b = src.index("\n                }\n", a) + len("\n                }\n")
    return src[a:b]


def test_mod_sync_never_asks_a_proxied_host_for_its_state(tmp_path):
    """Round 1b N6: mod-sync reads and writes a target's /state; for a host
    reached through this broker it asks nothing (the prefix never relays
    /state) and reports the push as skipped, while a direct host is read."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
peerAnswer = { status: 200, body: { rev: 1, settings: {}, layout: {} } };
const viaRead = await readState(hostById('rb'));
const viaWrite = await writeSettings(hostById('rb'), [{ key: 'k', value: 1 }]);
const viaCalls = calls.length;
const direct = await readState(hostById('rd'));
return { viaRead: viaRead, viaWrite: viaWrite, viaCalls: viaCalls,
         direct: !!direct, urls: calls.map(c => c.url) };
""".replace("INFO", json.dumps(INFO)),
        extra_src="const CLIENT_ID = 'cid';\n" + _mod_sync_fn("readState")
        + _mod_sync_fn("writeSettings"))
    assert out == {"viaRead": None, "viaWrite": "no_state", "viaCalls": 0,
                   "direct": True, "urls": ["http://d:4445/state"]}



@pytest.mark.parametrize("move", [TO_DIRECT, TO_C])
def test_a_login_probe_answered_after_a_switch_stores_nothing(tmp_path, move):
    """Round 1b N7: the record was switched while the password probe was
    out; the answer is about the old way in, so the password is not stored
    (on a now-direct record it would go straight to the host's url)."""
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
peerAnswer = { status: 200, body: [], delay: 60 };
authOverlayHostId = 'rb';
authTokenEl.value = 'NEW-B';
const p = _submit({ preventDefault() {} });
await new Promise(r => setTimeout(r, 15));
MOVE
await p;
return { msg: authErrorEl.textContent, token: hostById('rb').token,
         hidden: overlays.indexOf('hide') !== -1 };
""".replace("INFO", json.dumps(INFO)).replace("MOVE", move),
        extra_src=_dial_src() + _login_src())
    assert out == {"msg": "this host was switched meanwhile — enter its "
                          "password again",
                   "token": B_TOKEN, "hidden": False}


def test_a_login_probe_without_a_switch_still_stores(tmp_path):
    out = _run(tmp_path, _prefs(), """
infoAnswers = [INFO, INFO];
peerAnswer = { status: 200, body: [], delay: 30 };
authOverlayHostId = 'rb';
authTokenEl.value = 'NEW-B';
await _submit({ preventDefault() {} });
return hostById('rb').token;
""".replace("INFO", json.dumps(INFO)),
        extra_src=_dial_src() + _login_src() + "function refreshTaskbar() {}")
    assert out == "NEW-B"
