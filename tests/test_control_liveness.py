"""Behavioural tests for the /control liveness machinery in
``webterm/broker/64_js_sessions_poll_control.js`` (#248).

The single-active lease reaches a page over one ``/control`` WebSocket per
host. Before #248 nothing checked that socket was alive: a socket the browser
still believed OPEN (sleep, a network change, a black-holed path) never fired
``onclose``, never re-dialed, and the page stayed live and "active" while the
broker had released the lease and handed it to another device (the owner's
phone). The fix is a per-socket state machine: nonce'd pings, a pong deadline
only on sockets whose first status proves a ponging broker, make-before-break
replacement, a connect watchdog and a wake probe.

All of that is ordering across timers and socket events, so -- like
``tests/test_desktop_rpc.py`` -- this executes the SHIPPED section: cut AT
RUNTIME between its own section markers and evaluated in node inside a
``with (env)`` scope whose outer names are stubs: a fake WebSocket the script
opens, feeds and drops by hand, and a hand-cranked clock behind
``setTimeout``/``setInterval``/``Date.now``. One PASS/FAIL line per assert plus
a machine-readable tail; the fixture FAILS if a marker vanishes. Skipped when
node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from webterm.broker import ui

BROKER_DIR = Path(ui.__file__).resolve().parent
SRC_JS = BROKER_DIR / "64_js_sessions_poll_control.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

START = "        // ---- single-active-browser control channel (F-ACTIVECLIENT) -------"
END = "        // ---- desktop relay (#244)"

EXPECTED_ASSERTS = 93

_HARNESS = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(__SRC_PATH__, 'utf8');
const a = src.indexOf(__START__), b = src.indexOf(__END__);
if (a < 0 || b < a) throw new Error('section markers missing');
const slice = src.slice(a, b);
// #248 SH-1: the reply funnel lives in the desktop-relay section; cut just it.
const ra = src.indexOf('        function desktopReply(rec, ws, rid, payload) {');
const rb = src.indexOf('        // Broker-local int id (the DELIVERING connection');
if (ra < 0 || rb < ra) throw new Error('desktopReply boundary missing');
const replySrc = src.slice(ra, rb);

let pass = 0, fail = 0;
function ok(cond, label) {
    if (cond) { pass++; console.log('PASS: ' + label); }
    else { fail++; console.log('FAIL: ' + label); }
}

// ---- one world = one fresh evaluation of the section --------------------
function mkWorld(opts) {
    opts = opts || {};
    const w = { now: 1000000, tid: 1, timers: new Map(), sockets: [],
                log: [], visCbs: [], notices: [], rpcs: [], views: [],
                stickies: [], reclaims: [] };
    class FakeWS {
        constructor(url) {
            this.url = url; this.readyState = 0; this.sent = [];
            this.closed = false; w.sockets.push(this);
        }
        send(t) {
            this.attempts = (this.attempts || 0) + 1;
            if (this.readyState !== 1) throw new Error('not open');
            this.sent.push(JSON.parse(t));
        }
        close() { this.closed = true; this.readyState = 3; }
        _open() { this.readyState = 1; if (this.onopen) this.onopen(); }
        _recv(o) { if (this.onmessage) this.onmessage({ data: JSON.stringify(o) }); }
        _drop(code) {
            this.readyState = 3;
            if (this.onclose) this.onclose({ code: code || 1006 });
        }
        pings() { return this.sent.filter(m => m.type === 'ping'); }
        lastPing() { const p = this.pings(); return p[p.length - 1] || { n: -1 }; }
    }
    FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
    function setT(fn, ms, every) {
        const id = w.tid++;
        w.timers.set(id, { fn: fn, at: w.now + (ms || 0), every: every || 0 });
        return id;
    }
    w.advance = function (ms) {
        const end = w.now + ms;
        for (;;) {
            let next = null;
            for (const [id, t] of w.timers) {
                if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
            }
            if (!next) break;
            w.now = next[1].at;
            if (next[1].every) next[1].at += next[1].every;
            else w.timers.delete(next[0]);
            next[1].fn();
        }
        w.now = end;
    };
    const hosts = { local: { id: 'local', label: 'home' },
                    rb: { id: 'rb', label: 'remote B' } };
    const env = {
        WebSocket: FakeWS,
        setTimeout: function (fn, ms) { return setT(fn, ms, 0); },
        setInterval: function (fn, ms) { return setT(fn, ms, ms); },
        clearTimeout: function (id) { w.timers.delete(id); },
        clearInterval: function (id) { w.timers.delete(id); },
        Date: { now: function () { return w.now; } },
        CLIENT_ID: 'me',
        REATTACH_STABLE_MS: 5000,
        REATTACH_BACKOFF_MAX_MS: 30000,
        localHost: function () { return hosts.local; },
        hostById: function (id) { return hosts[id] || null; },
        hostWsUrl: function (h, p) { return 'ws://' + h.id + p; },
        resetControlBaselines: function (hid) { w.log.push('reset:' + hid); },
        controlKey: function (h, s) { return h + ':' + s; },
        applyControlView: function (k, m, src) { w.views.push(k + '/' + src); },
        handleDesktopRpc: function (hid, rec, ws, msg) {
            w.rpcs.push({ hid: hid, rid: msg.rid, live: rec.ws === ws, rec: rec, ws: ws });
        },
        desktopWsSend: function (ws, payload) {
            try { ws.send(JSON.stringify(payload)); } catch (_) {}
        },
        pollStateFor: function () { return w.poll || (w.poll = {}); },
        showAuthOverlay: function () { w.log.push('auth'); },
        renderHostStatus: function () {},
        showNotice: function (t, o) {
            w.notices.push(t);
            if (o && o.sticky) {
                // the real showNotice (66) dedupes identical sticky text
                if (w.stickies.some(n => !n.removed && n._noticeText === t)) return;
                w.stickies.push({ _noticeText: t, removed: false,
                                  remove: function () { this.removed = true; } });
            }
        },
        document: { getElementById: function (id) {
            return id !== 'notice-host' ? null : { querySelectorAll: function () {
                return w.stickies.filter(n => !n.removed);
            } };
        } },
        onVisibility: function (fn) { w.visCbs.push(fn); return function () {}; },
        registerHostCache: function () {},
        _leaseAffordance: { armed: false, asked: false },
        _homeActive: false,
        _booted: true,
        _deactivated: false,
        bootActiveView: function () { w.log.push('boot'); env._deactivated = false; },
        rebuildView: function () { w.log.push('rebuild'); env._deactivated = false; },
        teardownView: function () { w.log.push('teardown'); env._deactivated = true; },
        showBecomeActiveOverlay: function () { w.log.push('overlay'); },
        hideBecomeActiveOverlay: function () {},
        _emitModEvent: function (n, p) { w.log.push('lease:' + p.active); },
        onRemoteControlStatus: function (hid, active) { w.log.push('remote:' + hid + ':' + active); },
        reclaimHostSize: function (hid) { w.reclaims.push(hid); },
    };
    if (opts.env) Object.assign(env, opts.env);
    w.env = env;
    const factory = new Function('env',
        'with (env) {\n' + slice + '\n' + replySrc + '\n' +
        'var __api = { openControlWs: openControlWs, closeControlWs: closeControlWs,\n' +
        '  controlSockets: controlSockets, sendBecomeActive: sendBecomeActive,\n' +
        '  onControlStatus: onControlStatus, probeControl: probeControl,\n' +
        '  desktopReply: desktopReply };\n' +
        '}\nreturn __api;\n');
    w.api = factory(env);
    w.rec = function (hid) { return w.api.controlSockets.get(hid || 'local'); };
    w.last = function () { return w.sockets[w.sockets.length - 1]; };
    return w;
}
const ST = function (active, extra) {
    return Object.assign({ type: 'status', active: active,
        activeClientId: active ? 'me' : 'other', gen: 1, bootId: 'b1' }, extra || {});
};
const J = JSON.stringify;
const OLD_ST = function (active) {      // a pre-#248 broker: no gen / bootId
    return { type: 'status', active: active, activeClientId: active ? 'me' : 'other' };
};
// Open the home channel on a capable broker and answer its first status.
function connected(w, status) {
    w.api.openControlWs(w.env.localHost());
    const ws = w.last();
    ws._open();
    ws._recv(status || ST(true));
    return ws;
}
function pong(ws, n, extra) { ws._recv(Object.assign({ type: 'pong', n: n }, extra || {})); }

// ---- A2: pings, the deadline, make-before-break ---------------------------
let w = mkWorld();
let s0 = connected(w);
ok(s0.pings().length === 1 && Number.isInteger(s0.pings()[0].n),
    'L1 the first ping goes out at open, with an integer nonce');
pong(s0, s0.lastPing().n);
w.advance(25000);
ok(s0.pings().length === 2 && s0.pings()[1].n !== s0.pings()[0].n,
    'L2 a ping per CONTROL_PING_MS, each with a fresh nonce');

w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last(); s0._open(); s0._recv(OLD_ST(true));
w.advance(120000);
ok(w.sockets.length === 1 && !s0.closed,
    'L3 an old broker (no gen/bootId in its first status) gets no deadline: no replacement, ever');

w = mkWorld();
s0 = connected(w);
w.advance(9999);
ok(w.sockets.length === 1, 'L4 no replacement before the 10 s deadline');
w.advance(1);
ok(w.sockets.length === 2 && !s0.closed && w.rec().ws === s0,
    'L5 a missed pong dials a replacement and keeps the old socket in charge');
const c0 = w.last();
s0._recv({ type: 'terminal_control', id: 7, state: 'free', gen: 3 });
s0._recv({ type: 'desktop_rpc', rid: 41, op: 'get', args: {} });
ok(w.views.length === 1 && w.rpcs.length === 1 && w.rpcs[0].live === true,
    'L6 during the overlap the old socket still handles terminal_control and desktop_rpc (and may reply)');
c0._open();
ok(!s0.closed && w.rec().ws === s0,
    'L7 the replacement opening alone does not retire the old socket');
c0._recv(ST(true));
ok(w.rec().ws === c0 && s0.closed && w.rec().cand === null,
    'L8 the replacement first status promotes it and closes the old socket');
s0._recv(ST(false));
ok(w.log.indexOf('teardown') === -1,
    'L9 a late frame on the retired socket is ignored');

w = mkWorld();
s0 = connected(w);
w.advance(5000);
pong(s0, 999999);
w.advance(5000);
ok(w.sockets.length === 2,
    'L10 a pong with the wrong nonce does not satisfy the deadline');

w = mkWorld();
s0 = connected(w);
for (let i = 0; i < 4; i++) { pong(s0, s0.lastPing().n); w.advance(25000); }
ok(w.sockets.length === 1 && !s0.closed,
    'L11 answered pings never trigger a replacement');

w = mkWorld();
s0 = connected(w);
w.advance(10000);                       // miss 1 -> replacement dialed
let c1 = w.last();
c1._drop(1006);                         // the replacement fails before reporting
ok(!s0.closed && w.rec().ws === s0 && w.rec().cand === null,
    'L12 a failed replacement leaves the old socket open and in charge');
pong(s0, s0.lastPing().n);
for (let i = 0; i < 75; i++) { w.advance(1000); pong(s0, s0.lastPing().n); }
ok(!s0.closed && w.rec().ws === s0 && w.sockets.length === 2,
    'L13 an old socket that answers again simply stays');

w = mkWorld();
s0 = connected(w);
w.advance(10000);                       // miss 1
c1 = w.last(); c1._drop(1006);          // replacement fails
w.advance(10000);                       // miss 2 on the old socket
ok(s0.closed && w.rec().ws === null && w.rec().reattachTimer !== null,
    'L14 a second missed deadline retires the old socket and schedules a re-dial');

// M1: the old socket's close must not orphan the replacement.
w = mkWorld();
s0 = connected(w);
w.advance(10000);
c1 = w.last(); c1._open();
s0._drop(1006);                          // old one finally closes, mid-overlap
ok(w.rec().cand && w.rec().cand.ws === c1 && w.rec().reattachTimer === null
    && w.sockets.length === 2,
    'L15 the old socket closing mid-overlap leaves the replacement pending (no third dial)');
c1._recv(ST(true));
ok(w.rec().ws === c1 && w.log.indexOf('lease:true') !== -1,
    'L16 ...and the replacement status is then handled with the replacement in charge');
pong(c1, c1.lastPing().n);
w.advance(30000);
pong(c1, c1.lastPing().n);
w.advance(9000);
ok(w.sockets.length === 2 && !c1.closed && c1.pings().length === 2,
    'L17 the promoted socket runs its own pinger (no stray deadline from the old one)');

// M2: the connect watchdog.
w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last();                          // stays CONNECTING
w.advance(5000);
ok(s0.closed && w.rec().ws === null && w.rec().reattachTimer !== null,
    'L18 a dial still CONNECTING after 5 s is abandoned and a re-dial scheduled');
w.advance(2000);
ok(w.sockets.length === 2, 'L19 ...which dials again after the backoff');

// closeControlWs closes both.
w = mkWorld();
s0 = connected(w);
w.advance(10000);
c1 = w.last();
w.api.closeControlWs('local');
const closedNow = s0.closed && c1.closed;
w.advance(120000);
ok(closedNow && w.sockets.length === 2 && !w.rec(),
    'L20 closeControlWs closes the socket in charge AND the replacement; nothing re-dials');

// The wake probe.
w = mkWorld();
s0 = connected(w);
pong(s0, s0.lastPing().n);
w.advance(30000);                       // the 25 s ping is outstanding (thaw)
const before = s0.pings().length;
const oldN = s0.lastPing().n;
w.visCbs.forEach(fn => fn());
ok(s0.pings().length === before + 1 && s0.lastPing().n !== oldN,
    'L21 the wake probe sends a fresh ping at once');
pong(s0, oldN);                         // a late pong to the ping sent BEFORE it
w.advance(10000);
ok(w.sockets.length === 2,
    'L22 a late pong to an older ping does not satisfy the wake probe');

w = mkWorld();
s0 = connected(w);
pong(s0, s0.lastPing().n);
w.advance(20000);
w.visCbs.forEach(fn => fn());           // probe at t+20: deadline t+30
w.advance(5000);                        // the periodic ping goes out at t+25
ok(s0.pings().length === 3, 'L25a precondition: a periodic ping went out inside the probe window');
w.advance(5000);
ok(w.sockets.length === 2,
    'L25 a periodic ping never pushes a running deadline out');

w = mkWorld();
s0 = connected(w);
s0._drop(1006);
ok(w.rec().reattachTimer !== null && w.sockets.length === 1,
    'L23 precondition: a dropped channel waits out its backoff');
w.visCbs.forEach(fn => fn());
ok(w.sockets.length === 2 && w.rec().reattachTimer === null,
    'L24 the wake probe re-dials a backed-off channel at once');

// ---- the pong carries the live lease (M6) -----------------------------------
w = mkWorld();
s0 = connected(w, ST(true));
pong(s0, s0.lastPing().n, { active: true, activeClientId: 'me', gen: 1, bootId: 'b1' });
ok(w.log.filter(x => x === 'teardown' || x === 'rebuild').length === 0,
    'P1 a pong repeating the held lease runs no transition');
pong(s0, s0.lastPing().n, { active: false, activeClientId: 'phone', gen: 2, bootId: 'b1' });
ok(w.log.indexOf('teardown') !== -1 && w.env._homeActive === false,
    'P2 a pong saying the lease moved tears the view down (a missed status is corrected)');
const tdCount = w.log.filter(x => x === 'teardown').length;
pong(s0, s0.lastPing().n, { active: false, activeClientId: 'phone', gen: 2, bootId: 'b1' });
ok(w.log.filter(x => x === 'teardown').length === tdCount,
    'P3 a repeated inactive pong tears down nothing more');
const logLen = w.log.length;
pong(s0, s0.lastPing().n);
ok(w.log.length === logLen, 'P4 a pong without lease fields routes nothing');
w = mkWorld();
w.api.openControlWs(w.env.hostById('rb'));
s0 = w.last(); s0._open(); s0._recv(ST(true));
pong(s0, s0.lastPing().n, { active: false, activeClientId: 'phone', gen: 4, bootId: 'bB' });
ok(w.log.indexOf('remote:rb:false') !== -1,
    'P5 a remote host pong lease reaches that host remote handler');

// ---- A4: hints probe the broker; the broker decides ----------------------
w = mkWorld();
s0 = connected(w, ST(true));
pong(s0, s0.lastPing().n);
let pc = s0.pings().length;
w.api.probeControl('local');
ok(s0.pings().length === pc + 1,
    'H1 a hint on a healthy channel pings the broker at once');
w.api.probeControl('local');
w.api.probeControl('local');
ok(s0.pings().length === pc + 1,
    'H2 more hints while that probe is in flight send nothing (in-flight guard)');
pong(s0, s0.lastPing().n, { active: false, activeClientId: 'phone', gen: 2, bootId: 'b1' });
ok(w.log.indexOf('teardown') !== -1,
    'H3 the probe pong saying the lease moved tears the stale view down');

w = mkWorld();
s0 = connected(w, ST(true));
pong(s0, s0.lastPing().n);
w.api.probeControl('local');
pong(s0, s0.lastPing().n, { active: true, activeClientId: 'me', gen: 1, bootId: 'b1' });
ok(w.log.indexOf('teardown') === -1 && w.log.indexOf('overlay') === -1,
    'H4 a probe tears nothing down by itself (the broker said we still hold it)');

w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // missed -> replacement dialing
pc = s0.pings().length;
const sk = w.sockets.length;
w.api.probeControl('local');
ok(s0.pings().length === pc && w.sockets.length === sk,
    'H5 a hint while a replacement is dialing does nothing more');

w = mkWorld();
s0 = connected(w, ST(true));
s0._drop(1006);
w.api.probeControl('local');
ok(w.sockets.length === 2 && w.rec().reattachTimer === null,
    'H6 a hint with no channel brings the backed-off re-dial forward');

w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last(); s0._open(); s0._recv(OLD_ST(true));
pc = s0.pings().length;
w.api.probeControl('local');
ok(s0.pings().length === pc, 'H7 a hint on an older (non-ponging) broker sends nothing');

w = mkWorld();
s0 = connected(w, ST(true));
s0._drop(4401);
w.api.probeControl('local');
ok(w.sockets.length === 1, 'H8 a hint never re-dials a channel parked on 4401 (auth)');

// ---- B1: take back while reconnecting --------------------------------------
const BA = function (ws) { return ws.sent.filter(m => m.type === 'become_active').length; };
w = mkWorld();
s0 = connected(w, ST(false));
s0._drop(1006);                          // channel down, waiting out its backoff
w.api.sendBecomeActive(w.env.localHost());
ok(BA(s0) === 0 && w.notices.some(n => n.indexOf('will take over when connected') !== -1)
    && w.sockets.length === 2,
    'B1 a click with the channel down sends nothing, says so, and re-dials at once');
let s1 = w.last(); s1._open(); s1._recv(ST(false));
ok(BA(s1) === 1, 'B2 the new channel first inactive status sends become_active once');
s1._recv(ST(false));
ok(BA(s1) === 1, 'B3 the deferred send never re-arms: a second inactive status sends nothing');

w = mkWorld();
s0 = connected(w, ST(false));
s0._drop(1006);
w.api.sendBecomeActive(w.env.localHost());
w.advance(16000);                        // the re-dial is slow: the click is stale
s1 = w.last(); s1._open(); s1._recv(ST(false));
ok(BA(s1) === 0, 'B4 an intent older than 15 s sends nothing');

w = mkWorld();
s0 = connected(w, ST(false));
pong(s0, s0.lastPing().n);
w.api.sendBecomeActive(w.env.localHost());
ok(BA(s0) === 1, 'B5 a click on a live channel sends become_active at once');

w = mkWorld();
s0 = connected(w, ST(false));
s0._drop(1006);
w.api.sendBecomeActive(w.env.localHost());
s1 = w.last(); s1._open();
s1._recv(ST(true));                      // auto-claim on reconnect (S9)
s1._recv(ST(false, { activeClientId: 'phone', gen: 2 }));   // a later takeover
ok(BA(s1) === 0, 'B6 an active status clears the intent: a later takeover is not fought (S9)');

w = mkWorld();
w.api.sendBecomeActive(w.env.hostById('rb'));
ok(w.sockets.length === 1 && w.sockets[0].url.indexOf('ws://rb/control') === 0,
    'B7 a click for a host with no channel yet dials it');
s1 = w.last();
if (s1) { s1._open(); s1._recv(ST(false)); }
ok(!!s1 && BA(s1) === 1, 'B8 ...and takes over when it reports');

// ---- B2: the lost-contact notice (O2) --------------------------------------
const LOST = 'lost contact with this broker — retrying…';
const lostUp = function (w) { return w.stickies.filter(n => !n.removed && n._noticeText === LOST).length; };
w = mkWorld();
s0 = connected(w, ST(true));
s0._drop(1006);
w.advance(9999);
ok(lostUp(w) === 0, 'N1 no notice before 10 s without a home channel');
w.advance(1);
// the backoff re-dial (2 s) is still CONNECTING: no status yet
ok(lostUp(w) === 1, 'N2 after 10 s without a home channel the sticky notice is up');
s1 = w.last(); s1._open(); s1._recv(ST(true));
ok(lostUp(w) === 0, 'N3 the next home status takes it down');

w = mkWorld();
s0 = connected(w, ST(true));
s0._drop(1006);
w.advance(2000);
s1 = w.last(); s1._open(); s1._recv(ST(true));
for (let i = 0; i < 20; i++) { w.advance(1000); pong(s1, s1.lastPing().n); }
ok(lostUp(w) === 0, 'N4 a channel back within 10 s shows nothing');

w = mkWorld();
w.api.openControlWs(w.env.hostById('rb'));
s0 = w.last(); s0._open(); s0._recv(ST(true));
s0._drop(1006);
w.advance(20000);
ok(lostUp(w) === 0, 'N5 a remote host drop shows nothing');

w = mkWorld();
w.api.openControlWs(w.env.localHost());   // never opens: the startup guard owns it
w.advance(20000);
ok(lostUp(w) === 0, 'N6 a channel that never opened shows nothing');

w = mkWorld();
s0 = connected(w, ST(true));
s0._drop(4401);
w.advance(20000);
ok(lostUp(w) === 0, 'N7 a 4401 park shows nothing (the auth overlay owns it)');

w = mkWorld();
s0 = connected(w, ST(true));
w.advance(20000);                        // two missed deadlines: retired
ok(s0.closed, 'N8a precondition: a silent channel is retired after two deadlines');
w.advance(10000);
ok(lostUp(w) === 1, 'N8 ...and 10 s later, still down, the notice is up');

// ---- C1: reclaim once per (host, bootId, gen) newly held ------------------
w = mkWorld();
s0 = connected(w, ST(true, { gen: 5 }));
ok(J(w.reclaims) === J(['local']), 'C1 a first holding reclaims that host once');
s0._recv(ST(true, { gen: 5 }));
pong(s0, s0.lastPing().n, { active: true, activeClientId: 'me', gen: 5, bootId: 'b1' });
ok(w.reclaims.length === 1, 'C2 a repeated status or pong of the same holding reclaims nothing');
w.advance(10000);                        // A2 replacement
s1 = w.last(); s1._open(); s1._recv(ST(true, { gen: 5 }));
ok(w.rec().ws === s1 && w.reclaims.length === 1,
    'C3 an A2 replacement reporting the same holding reclaims nothing');
s1._recv(ST(false, { activeClientId: 'phone', gen: 6 }));
s1._recv(ST(true, { gen: 7 }));
ok(J(w.reclaims) === J(['local', 'local']),
    'C4 losing then re-acquiring (a new gen) reclaims again');

w = mkWorld();
s0 = connected(w, ST(true, { gen: 5 }));
s0._drop(1006);
w.advance(2000);
s1 = w.last(); s1._open(); s1._recv(ST(true, { gen: 6 }));
ok(w.log.indexOf('teardown') === -1 && w.reclaims.length === 2,
    'C5 a silent re-acquire (never torn down, auto-claim bumped gen) reclaims (S1)');

w = mkWorld();
s0 = connected(w, ST(true, { gen: 1, bootId: 'b1' }));
s0._drop(1006);
w.advance(2000);
s1 = w.last(); s1._open(); s1._recv(ST(true, { gen: 1, bootId: 'b2' }));
ok(w.reclaims.length === 2, 'C6 a broker restart (new bootId, same gen number) reclaims');

w = mkWorld();
s0 = connected(w, ST(false));
ok(w.reclaims.length === 0, 'C7 an inactive status never reclaims');

w = mkWorld();
w.api.openControlWs(w.env.hostById('rb'));
s0 = w.last(); s0._open(); s0._recv(ST(true, { gen: 9, bootId: 'bB' }));
ok(J(w.reclaims) === J(['rb']),
    'C8 a remote host auto-claim reclaims that host, with no mask involved (M3)');

w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last(); s0._open(); s0._recv(OLD_ST(true));
ok(w.reclaims.length === 0, 'C9 an older broker (no gen/bootId) never reclaims');

// ---- codex review round 1 ----------------------------------------------------
w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // miss 1 -> replacement
s1 = w.last(); s1._open();               // opens, never reports
s0._drop(1006);                          // the old one dies meanwhile
w.advance(5000);
ok(s1.closed && w.rec().cand === null && w.rec().reattachTimer !== null,
    'K1 an OPEN replacement that never reports is abandoned at 5 s and the host re-dials (no deadlock)');

w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last(); s0._open();               // opens, never reports
w.advance(5000);
ok(s0.closed && w.rec().ws === null && w.rec().reattachTimer !== null,
    'K2 an OPEN first socket that never reports is abandoned at 5 s too');

w = mkWorld();
s0 = connected(w, ST(true, { gen: 5 }));
w.advance(10000);                        // replacement dialing
s1 = w.last(); s1._open();
s0._recv(ST(false, { activeClientId: 'phone', gen: 6 }));   // old socket: the flip
const tdBefore = w.log.filter(x => x === 'teardown').length;
s1._recv(ST(true, { gen: 5 }));          // replacement: its older connect status
ok(w.rec().ws === s1 && w.log.filter(x => x === 'rebuild').length === 0
    && w.env._homeActive === false && tdBefore === 1,
    'K3 a replacement older-gen first status promotes it but cannot undo the newer flip');

w = mkWorld();
s0 = connected(w, ST(true));
const resetsAtOpen = w.log.filter(x => x === 'reset:local').length;
w.advance(10000);
s1 = w.last(); s1._open();
ok(w.log.filter(x => x === 'reset:local').length === resetsAtOpen,
    'K4a a replacement opening does not reset the #239 control baselines');
s1._recv(ST(true));
ok(w.rec().ws === s1 && w.log.filter(x => x === 'reset:local').length === resetsAtOpen,
    'K4 ...nor does its promotion on the SAME broker run (the gen order stands)');
w = mkWorld();
s0 = connected(w, ST(true, { bootId: 'b1' }));
const r0 = w.log.filter(x => x === 'reset:local').length;
w.advance(10000);
s1 = w.last(); s1._open(); s1._recv(ST(true, { bootId: 'b2' }));
ok(w.log.filter(x => x === 'reset:local').length === r0 + 1,
    'K4b a replacement that reports a NEW broker run resets the baselines once');

w = mkWorld();
s0 = connected(w, ST(true));
w.advance(19999);
ok(lostUp(w) === 0 && !s0.closed, 'K5a precondition: no notice before 10 s after the first missed deadline');
w.advance(1);
ok(lostUp(w) === 1, 'K5 a silent channel shows the notice 10 s after its FIRST missed deadline');

w = mkWorld();
s0 = connected(w, ST(true, { gen: 40, bootId: 'b1' }));
s0._drop(1006);
w.advance(2000);
s1 = w.last(); s1._open(); s1._recv(ST(false, { activeClientId: 'phone', gen: 2, bootId: 'b2' }));
ok(w.log.indexOf('teardown') !== -1,
    'K6 a restarted broker (new bootId, lower gen) is not mistaken for a stale status');

// ---- manager note: 1012 re-dials at once; pongs are ordered too -----------
w = mkWorld();
s0 = connected(w, ST(true));
for (let i = 0; i < 6; i++) { w.advance(1000); pong(s0, s0.lastPing().n); }
s0._drop(1012);
ok(w.sockets.length === 2 && w.rec().reattachTimer === null,
    'Q1 a 1012 on a channel up for 5 s re-dials at once (no backoff)');
s1 = w.last(); s1._open(); s1._drop(1012);    // a fresh socket 1012s at once
ok(w.sockets.length === 2 && w.rec().reattachTimer !== null,
    'Q2 a 1012 on a socket opened under 5 s ago backs off (no tight loop)');

w = mkWorld();
s0 = connected(w, ST(true));
for (let i = 0; i < 6; i++) { w.advance(1000); pong(s0, s0.lastPing().n); }
s0._drop(1006);
ok(w.sockets.length === 1 && w.rec().reattachTimer !== null,
    'Q3 any other close still backs off');

w = mkWorld();
s0 = connected(w, ST(true, { gen: 5 }));
w.advance(10000);
s1 = w.last(); s1._open(); s1._recv(ST(false, { activeClientId: 'phone', gen: 6 }));
const td = w.log.filter(x => x === 'teardown').length;
pong(s1, s1.lastPing().n, { active: true, activeClientId: 'me', gen: 5, bootId: 'b1' });
ok(td === 1 && w.log.filter(x => x === 'rebuild').length === 0 && w.env._homeActive === false,
    'Q4 an older-gen pong (any socket) cannot undo a newer status');

// ---- codex review round 2 ----------------------------------------------------
w = mkWorld();
w.api.openControlWs(w.env.localHost());
s0 = w.last();
w.advance(4800);                         // a slow upgrade
s0._open();
w.advance(4000);
ok(!s0.closed, 'W1 a slow upgrade still gets a full 5 s for its first status');
s0._recv(ST(true));
w.advance(2000);
ok(!s0.closed && w.rec().ws === s0, 'W2 ...and the status arriving keeps it');

w = mkWorld();
s0 = connected(w, ST(true));             // opened at t0, never answers its first ping
w.advance(10000);                        // miss 1 at t+10 -> replacement
s1 = w.last(); s1._open(); s1._drop(1006);   // it opens, then fails
pong(s0, s0.lastPing().n);               // the original answers its second chance
w.advance(1000);
ok(w.sockets.length === 2 && !s0.closed && w.rec().cand === null,
    'Q5a precondition: a replacement opened and failed; the long-lived socket is in charge');
s0._drop(1012);
ok(w.rec().reattachTimer === null && w.sockets.length === 3,
    'Q5 the 1012 stability test reads the closing socket, not a replacement that just opened');

// ---- w1-4 SH-3: the lost-contact notice never sits beside the auth overlay ----
w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // miss 1 arms the 10 s count
s0._drop(4401);                          // then the token is refused
w.advance(10000);
ok(lostUp(w) === 0 && w.log.indexOf('auth') !== -1,
    'SH3a miss -> 4401 -> +10 s: the auth overlay, no lost-contact notice');

w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // miss 1 arms the count
w.poll = { authNeeded: true };           // a 401 elsewhere (the /sessions poll)
w.advance(10000);
ok(lostUp(w) === 0, 'SH3b an armed count skips while the host is waiting on a password');

w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // miss 1 arms the count
s0._drop(4401);
w.poll.authNeeded = false;               // the user logs in before the re-dial reports
w.advance(10000);
ok(lostUp(w) === 0, 'SH3c the 4401 itself cancels the armed count');

// ---- w1-4 NIT-7: a replacement's 4401 goes to the auth flow at once --------
w = mkWorld();
s0 = connected(w, ST(true));
w.advance(10000);                        // miss 1 -> replacement dialing
s1 = w.last();
s1._drop(4401);                          // the REPLACEMENT is refused
ok(w.log.indexOf('auth') !== -1 && w.poll && w.poll.authNeeded === true,
    'N7a a replacement 4401 shows the auth overlay at once');
ok(s0.closed && w.rec().ws === null && w.rec().cand === null && w.rec().reattachTimer === null,
    'N7b ...retires the old socket too and schedules no re-dial');
const nAfter = w.sockets.length;
w.advance(60000);
ok(w.sockets.length === nAfter, 'N7c ...and nothing re-dials a broker that needs a password');

// ---- w1-4 SH-1: a reply survives a promotion on the same broker run ---------
const RESULTS = function (ws) { return ws.sent.filter(m => m.type === 'desktop_result'); };
function promoteMidRpc(bootOfReplacement) {
    const w = mkWorld();
    const s0 = connected(w, ST(true, { bootId: 'b1' }));
    pong(s0, s0.lastPing().n);
    s0._recv({ type: 'desktop_rpc', rid: 7, op: 'focus', args: {} });   // the op starts
    const call = w.rpcs[0];
    w.advance(25000);                    // next ping, unanswered
    w.advance(10000);                    // miss -> replacement
    const s1 = w.last(); s1._open(); s1._recv(ST(true, { bootId: bootOfReplacement }));
    w.api.desktopReply(call.rec, call.ws, 7, { ok: true });          // the op finishes
    return { w: w, s0: s0, s1: s1 };
}
let pm = promoteMidRpc('b1');
ok(pm.w.rec().ws === pm.s1 && pm.s0.closed, 'R0 precondition: the delivering socket was promoted away mid-rpc');
ok(RESULTS(pm.s1).length === 1 && RESULTS(pm.s1)[0].rid === 7 && RESULTS(pm.s0).length === 0,
    'R1 same broker run: the reply goes out on the socket now in charge');
pm = promoteMidRpc('b2');
ok(RESULTS(pm.s1).length === 0 && RESULTS(pm.s0).length === 0,
    'R2 a different broker run (bootId): the reply is dropped');
w = mkWorld();
s0 = connected(w, ST(true));
s0._recv({ type: 'desktop_rpc', rid: 9, op: 'get', args: {} });
w.api.desktopReply(w.rpcs[0].rec, w.rpcs[0].ws, 9, { ok: true });
ok(RESULTS(s0).length === 1, 'R3 no promotion: the reply rides the delivering socket (unchanged)');
pm = promoteMidRpc('b1');
w = pm.w;
s0 = pm.s0; s1 = pm.s1;
s1.sent.length = 0;
s1._drop(1006);                          // the socket in charge is gone too
const att0 = s0.attempts || 0, att1 = s1.attempts || 0;
w.api.desktopReply(w.rpcs[0].rec, w.rpcs[0].ws, 7, { ok: true });
ok(RESULTS(s1).length === 0 && (s0.attempts || 0) === att0 && (s1.attempts || 0) === att1,
    'R4 no socket in charge: dropped (no send attempted on any socket)');

console.log(pass + ' passed, ' + fail + ' failed');
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    src = SRC_JS.read_text(encoding="utf-8")
    for m in (START, END):
        assert src.count(m) == 1, f"marker {m!r} must occur exactly once"
    harness = tmp_path_factory.mktemp("ctl-liveness") / "harness.js"
    harness.write_text(
        _HARNESS.replace("__SRC_PATH__", json.dumps(str(SRC_JS)))
        .replace("__START__", json.dumps(START))
        .replace("__END__", json.dumps(END)),
        encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$", lines[-1] if lines else "")
    assert tail is not None, (
        f"no machine-readable tail\nrc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}")
    results = {}
    for line in lines[:-1]:
        if line.startswith("PASS: "):
            results[line[6:]] = True
        elif line.startswith("FAIL: "):
            results[line[6:]] = False
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "passed": int(tail.group(1)), "failed": int(tail.group(2)),
            "results": results}


def _ok(run, label):
    got = run["results"].get(label)
    assert got is True, (
        f"{label!r} {'failed' if got is False else 'never ran'}\n"
        f"stdout: {run['stdout']}\nstderr: {run['stderr']}")


@pytest.mark.parametrize("label", [
    "L1 the first ping goes out at open, with an integer nonce",
    "L2 a ping per CONTROL_PING_MS, each with a fresh nonce",
    "L3 an old broker (no gen/bootId in its first status) gets no deadline: no replacement, ever",
    "L4 no replacement before the 10 s deadline",
    "L5 a missed pong dials a replacement and keeps the old socket in charge",
    "L6 during the overlap the old socket still handles terminal_control and desktop_rpc (and may reply)",
    "L7 the replacement opening alone does not retire the old socket",
    "L8 the replacement first status promotes it and closes the old socket",
    "L9 a late frame on the retired socket is ignored",
    "L10 a pong with the wrong nonce does not satisfy the deadline",
    "L11 answered pings never trigger a replacement",
    "L12 a failed replacement leaves the old socket open and in charge",
    "L13 an old socket that answers again simply stays",
    "L14 a second missed deadline retires the old socket and schedules a re-dial",
])
def test_liveness_deadline_and_make_before_break(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "L15 the old socket closing mid-overlap leaves the replacement pending (no third dial)",
    "L16 ...and the replacement status is then handled with the replacement in charge",
    "L17 the promoted socket runs its own pinger (no stray deadline from the old one)",
    "L18 a dial still CONNECTING after 5 s is abandoned and a re-dial scheduled",
    "L19 ...which dials again after the backoff",
    "L20 closeControlWs closes the socket in charge AND the replacement; nothing re-dials",
])
def test_per_socket_state_watchdog_and_close(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "L21 the wake probe sends a fresh ping at once",
    "L22 a late pong to an older ping does not satisfy the wake probe",
    "L23 precondition: a dropped channel waits out its backoff",
    "L24 the wake probe re-dials a backed-off channel at once",
    "L25a precondition: a periodic ping went out inside the probe window",
    "L25 a periodic ping never pushes a running deadline out",
])
def test_wake_probe(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "P1 a pong repeating the held lease runs no transition",
    "P2 a pong saying the lease moved tears the view down (a missed status is corrected)",
    "P3 a repeated inactive pong tears down nothing more",
    "P4 a pong without lease fields routes nothing",
    "P5 a remote host pong lease reaches that host remote handler",
])
def test_pong_lease_goes_through_on_control_status(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "H1 a hint on a healthy channel pings the broker at once",
    "H2 more hints while that probe is in flight send nothing (in-flight guard)",
    "H3 the probe pong saying the lease moved tears the stale view down",
    "H4 a probe tears nothing down by itself (the broker said we still hold it)",
    "H5 a hint while a replacement is dialing does nothing more",
    "H6 a hint with no channel brings the backed-off re-dial forward",
    "H7 a hint on an older (non-ponging) broker sends nothing",
    "H8 a hint never re-dials a channel parked on 4401 (auth)",
])
def test_probe_control(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "B1 a click with the channel down sends nothing, says so, and re-dials at once",
    "B2 the new channel first inactive status sends become_active once",
    "B3 the deferred send never re-arms: a second inactive status sends nothing",
    "B4 an intent older than 15 s sends nothing",
    "B5 a click on a live channel sends become_active at once",
    "B6 an active status clears the intent: a later takeover is not fought (S9)",
    "B7 a click for a host with no channel yet dials it",
    "B8 ...and takes over when it reports",
])
def test_take_back_while_reconnecting(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "N1 no notice before 10 s without a home channel",
    "N2 after 10 s without a home channel the sticky notice is up",
    "N3 the next home status takes it down",
    "N4 a channel back within 10 s shows nothing",
    "N5 a remote host drop shows nothing",
    "N6 a channel that never opened shows nothing",
    "N7 a 4401 park shows nothing (the auth overlay owns it)",
    "N8a precondition: a silent channel is retired after two deadlines",
    "N8 ...and 10 s later, still down, the notice is up",
])
def test_lost_contact_notice(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "C1 a first holding reclaims that host once",
    "C2 a repeated status or pong of the same holding reclaims nothing",
    "C3 an A2 replacement reporting the same holding reclaims nothing",
    "C4 losing then re-acquiring (a new gen) reclaims again",
    "C5 a silent re-acquire (never torn down, auto-claim bumped gen) reclaims (S1)",
    "C6 a broker restart (new bootId, same gen number) reclaims",
    "C7 an inactive status never reclaims",
    "C8 a remote host auto-claim reclaims that host, with no mask involved (M3)",
    "C9 an older broker (no gen/bootId) never reclaims",
])
def test_reclaim_per_new_holding(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "K1 an OPEN replacement that never reports is abandoned at 5 s and the host re-dials (no deadlock)",
    "K2 an OPEN first socket that never reports is abandoned at 5 s too",
    "K3 a replacement older-gen first status promotes it but cannot undo the newer flip",
    "K4a a replacement opening does not reset the #239 control baselines",
    "K4 ...nor does its promotion on the SAME broker run (the gen order stands)",
    "K4b a replacement that reports a NEW broker run resets the baselines once",
    "K5a precondition: no notice before 10 s after the first missed deadline",
    "K5 a silent channel shows the notice 10 s after its FIRST missed deadline",
    "K6 a restarted broker (new bootId, lower gen) is not mistaken for a stale status",
])
def test_codex_round1(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "Q1 a 1012 on a channel up for 5 s re-dials at once (no backoff)",
    "Q2 a 1012 on a socket opened under 5 s ago backs off (no tight loop)",
    "Q3 any other close still backs off",
    "Q4 an older-gen pong (any socket) cannot undo a newer status",
])
def test_manager_note_1012_and_pong_order(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "W1 a slow upgrade still gets a full 5 s for its first status",
    "W2 ...and the status arriving keeps it",
    "Q5a precondition: a replacement opened and failed; the long-lived socket is in charge",
    "Q5 the 1012 stability test reads the closing socket, not a replacement that just opened",
])
def test_codex_round2(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "SH3a miss -> 4401 -> +10 s: the auth overlay, no lost-contact notice",
    "SH3b an armed count skips while the host is waiting on a password",
    "SH3c the 4401 itself cancels the armed count",
])
def test_w14_sh3_no_notice_beside_auth(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "N7a a replacement 4401 shows the auth overlay at once",
    "N7b ...retires the old socket too and schedules no re-dial",
    "N7c ...and nothing re-dials a broker that needs a password",
])
def test_w14_nit7_replacement_4401(run, label):
    _ok(run, label)


@pytest.mark.parametrize("label", [
    "R0 precondition: the delivering socket was promoted away mid-rpc",
    "R1 same broker run: the reply goes out on the socket now in charge",
    "R2 a different broker run (bootId): the reply is dropped",
    "R3 no promotion: the reply rides the delivering socket (unchanged)",
    "R4 no socket in charge: dropped (no send attempted on any socket)",
])
def test_w14_sh1_reply_across_promotion(run, label):
    _ok(run, label)


def test_every_scenario_passed_and_node_exited_clean(run):
    assert run["failed"] == 0, run["stdout"] + run["stderr"]
    assert run["passed"] == EXPECTED_ASSERTS, (
        f"expected {EXPECTED_ASSERTS} asserts, ran {run['passed']}\n{run['stdout']}")
    assert len(run["results"]) == EXPECTED_ASSERTS
    assert run["rc"] == 0, run["stderr"]
