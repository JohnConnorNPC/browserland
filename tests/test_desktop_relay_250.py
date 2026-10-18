"""gh #250, page track: the desktop relay's answered-rpc cache and lease
checks in ``webterm/broker/64_js_sessions_poll_control.js``.

Runs the SHIPPED fragment in node through the two existing harnesses, cut AT
RUNTIME (never copied): tests/test_desktop_rpc.py's ``_HARNESS`` world for
``handleDesktopRpc`` (cut before its own scenarios), and
tests/test_control_liveness.py's ``mkWorld`` for the /control status path.
Each harness keeps its own pinned assert count, so these scenarios live here.
Skipped when node is absent.
"""

import json
import re
import subprocess

import pytest

from tests import test_control_liveness as liveness
from tests import test_desktop_rpc as rpc

pytestmark = pytest.mark.skipif(rpc.NODE is None, reason="node not installed")

_RPC_CUT = "(async function () {"
_LIVE_CUT = "// ---- A2: pings, the deadline, make-before-break"

# Every ok() each script makes; pinned so a dropped scenario cannot pass by
# absence.
EXPECTED_RPC_ASSERTS = 25
EXPECTED_LIVE_ASSERTS = 2

_RPC_SCENARIOS = r"""
(async function () {
    const SS = new WeakMap();
    env._ctlSockState = SS;
    function sock(boot) {               // a socket whose first status named a run
        const w = mkSock(); SS.set(w, { bootId: boot }); return w;
    }
    async function call(hid, ws, msg, r) {
        await api.handleDesktopRpc(hid, r || { ws: ws }, ws, msg);
        return ws.sent[ws.sent.length - 1];
    }
    const F = function (id, extra) {
        return Object.assign({ op: 'focus', args: { id: id, visible: null } }, extra);
    };

    // K1 cross-host: the same rid on two brokers is two calls.
    resetWorld();
    await call('local', sock('bL'), { rid: 3, op: 'get', args: { visible: null } });
    const k1 = await call('remoteX', sock('bR'), { rid: 3, op: 'get', args: { visible: [5] } });
    const ids = ((k1 && k1.value && k1.value.windows) || []).map(function (x) { return x.id; });
    ok(JSON.stringify(ids) === '[5]',
        'K1 a remote get with a home rid answers its own scoped snapshot');

    // K2 cross-host mutation: both applied.
    resetWorld();
    await call('local', sock('bL'), Object.assign({ rid: 4 }, F(7)));
    await call('remoteX', sock('bR'), Object.assign({ rid: 4 }, F(5)));
    ok(countMut('reveal') === 2, 'K2 a remote focus with a home rid is applied');

    // K3 a restarted broker (the FRAME names the run; the sockets name none).
    resetWorld();
    await call('local', mkSock(), Object.assign({ rid: 1, bootId: 'run1' }, F(7)));
    await call('local', mkSock(), Object.assign({ rid: 1, bootId: 'run2' }, F(9)));
    ok(countMut('reveal') === 2, 'K3 a new run (frame bootId) re-using rid 1 is applied');

    // K4 the same run, the same rid on a sibling socket: deduped.
    resetWorld();
    await call('local', sock('run1'), Object.assign({ rid: 11 }, F(7)));
    const k4 = await call('local', sock('run1'), Object.assign({ rid: 11 }, F(7)));
    ok(countMut('reveal') === 1 && k4 && k4.ok === true,
        'K4 same run, same rid on another socket: applied once, answered twice');

    // K5 the key is the rpc's run, never rec.leaseBoot (which moves).
    resetWorld();
    const r5 = { leaseBoot: 'b1', leaseGen: 1 };
    let w5 = sock('b1'); r5.ws = w5;
    await call('local', w5, Object.assign({ rid: 40, bootId: 'b1' }, F(7)), r5);
    r5.leaseBoot = 'b2';                  // the host restarted meanwhile
    w5 = sock('b1'); r5.ws = w5;
    const k5a = await call('local', w5, Object.assign({ rid: 40, bootId: 'b1' }, F(7)), r5);
    ok(k5a && k5a.ok === true && countMut('reveal') === 1,
        'K5a a replay of the old run is served its first answer');
    w5 = sock('b2'); r5.ws = w5;
    await call('local', w5, Object.assign({ rid: 40, bootId: 'b2' }, F(9)), r5);
    ok(countMut('reveal') === 2, 'K5b the new run rid 40 is applied');

    // K6 a frame without a bootId (a #248 broker) keys on the socket's run.
    resetWorld();
    await call('local', sock('s1'), Object.assign({ rid: 50 }, F(7)));
    await call('local', sock('s2'), Object.assign({ rid: 50 }, F(9)));
    await call('local', sock('s2'), Object.assign({ rid: 50 }, F(9)));
    ok(countMut('reveal') === 2,
        'K6 no frame bootId: the delivering socket names the run');

    // D1 a duplicate while the first copy is still flushing.
    resetWorld();
    rearmPush(false);
    const d1a = sock('run1'), d1b = sock('run1');
    const p1 = api.handleDesktopRpc('local', { ws: d1a }, d1a,
        { rid: 60, op: 'float', args: { id: 7, visible: null } });
    await Promise.resolve(); await Promise.resolve();
    const p2 = api.handleDesktopRpc('local', { ws: d1b }, d1b,
        { rid: 60, op: 'float', args: { id: 7, visible: null } });
    await Promise.resolve(); await Promise.resolve();
    ok(d1b.sent.length === 0, 'D1a the in-flight duplicate waits for the first copy');
    pushResolve();
    await p1; await p2;
    ok(countMut('detachToFloat') === 1 && pushCalls === 1,
        'D1b the duplicate never re-applies the op');
    ok(d1a.sent.length === 1 && d1b.sent.length === 1
        && JSON.stringify(d1a.sent[0]) === JSON.stringify(d1b.sent[0])
        && d1a.sent[0].ok === true,
        'D1c both sockets get the same answer');
    rearmPush(true);

    // K7 the forget drops only that host's run-less answers. A second
    // evaluation of the same slice exposes _desktopForgetRun (own cache).
    resetWorld();
    const k7 = new Function('env', 'with (env) {\n' + slice + '\n' +
        'var __k7 = { h: handleDesktopRpc, forget: _desktopForgetRun };\n' +
        '}\nreturn __k7;\n')(env);
    async function call7(hid, ws, msg) {
        await k7.h(hid, { ws: ws }, ws, msg);
        return ws.sent[ws.sent.length - 1];
    }
    await call7('local', mkSock(), Object.assign({ rid: 70 }, F(7)));
    await call7('local', sock('bX'), Object.assign({ rid: 71 }, F(7)));
    await call7('remoteX', mkSock(), Object.assign({ rid: 72 }, F(5)));
    await call7('local', mkSock(), Object.assign({ rid: 70 }, F(7)));
    ok(countMut('reveal') === 3, 'K7a before the forget a run-less replay is deduped');
    k7.forget('local');
    await call7('local', mkSock(), Object.assign({ rid: 70 }, F(7)));
    ok(countMut('reveal') === 4, 'K7b after the forget the run-less rid is applied again');
    await call7('local', sock('bX'), Object.assign({ rid: 71 }, F(7)));
    await call7('remoteX', mkSock(), Object.assign({ rid: 72 }, F(5)));
    ok(countMut('reveal') === 4,
        'K7c a named run, and another host, keep their answers');

    // K8 a run-less op still in flight when the host's new socket reports:
    // the new run's rid 1 neither waits for it nor is overwritten by the
    // old op's LATER answer (the old op is a get held on its one await).
    resetWorld();
    let release8 = null;
    execNext = function () { return new Promise(function (res) { release8 = res; }); };
    const w8a = mkSock(), w8b = mkSock();
    const p8a = k7.h('local', { ws: w8a }, w8a,
        { rid: 1, op: 'get', args: { visible: null } });
    await Promise.resolve(); await Promise.resolve();
    k7.forget('local');                   // the restarted broker's first status
    execNext = null;
    await k7.h('local', { ws: w8b }, w8b,
        { rid: 1, op: 'float', args: { id: 9, visible: null } });
    ok(countMut('detachToFloat') === 1 && w8b.sent.length === 1 && w8a.sent.length === 0,
        'K8a the new run\'s rid 1 is applied while the old run\'s is in flight');
    release8({ ok: true, value: { active: 0, items: [] } });
    await p8a;
    const w8c = mkSock();
    await k7.h('local', { ws: w8c }, w8c,
        { rid: 1, op: 'float', args: { id: 9, visible: null } });
    ok(countMut('detachToFloat') === 1 && w8a.sent.length === 1
        && JSON.stringify(w8c.sent[0]) === JSON.stringify(w8b.sent[0]),
        'K8b a replay of the new run gets the new run\'s answer, not the old op\'s');

    // ---- N: the rpc's generation and run against this host's newest status
    function held(boot, gen) {          // rec + its socket, as after a status
        const w = sock(boot);
        return { r: { leaseBoot: boot, leaseGen: gen, ws: w }, w: w };
    }
    resetWorld();
    let h = held('bL', 5);
    let a = await call('local', h.w, Object.assign({ rid: 80, gen: 4 }, F(7)), h.r);
    ok(a && a.error === 'lost_lease' && countMut('reveal') === 0,
        'N1 an rpc of an older generation (same run) answers lost_lease, applies nothing');
    resetWorld();
    h = held('bL', 5);
    a = await call('local', h.w, Object.assign({ rid: 81, gen: 5 }, F(7)), h.r);
    ok(a && a.ok === true && countMut('reveal') === 1,
        'N2 an rpc of the current generation applies');
    resetWorld();
    h = held('bL', 5);
    h.r.leaseBoot = 'OTHER';
    a = await call('local', h.w, Object.assign({ rid: 82, gen: 5 }, F(7)), h.r);
    ok(a && a.error === 'lost_lease' && countMut('reveal') === 0,
        'N3 an rpc of a run that is not the newest status\'s answers lost_lease');
    resetWorld();
    h = held('bL', 9);
    a = await call('local', h.w, Object.assign({ rid: 83 }, F(7)), h.r);
    ok(a && a.ok === true, 'N4 a frame with no gen (older broker) skips the gen check');
    resetWorld();
    rearmPush(false);
    h = held('bL', 5);
    const pN5 = api.handleDesktopRpc('local', h.r, h.w,
        { rid: 84, op: 'float', gen: 5, args: { id: 7, visible: null } });
    await Promise.resolve(); await Promise.resolve();
    h.r.leaseGen = 6;                     // a newer status landed mid-flush
    pushResolve(); await pN5;
    a = h.w.sent[h.w.sent.length - 1];
    ok(a && a.error === 'lost_lease', 'N5 a newer generation seen during the flush answers lost_lease');
    rearmPush(true);
    resetWorld();
    h = held('bL', 5);
    a = await call('local', h.w, Object.assign({ rid: 85, gen: 4.5 }, F(7)), h.r);
    ok(a && a.ok === true, 'N6 a non-integer gen is treated as absent');

    // ---- H: a REMOTE host's lease, which never sets _deactivated
    const ps = {};
    env.pollStateFor = function (x) { return ps[x] || (ps[x] = {}); };
    resetWorld();
    ps.remoteX = { leaseInactive: true };
    a = await call('remoteX', sock('bR'), Object.assign({ rid: 90 }, F(5)));
    ok(a && a.error === 'lost_lease' && countMut('reveal') === 0,
        'H1 a host whose lease this page lost answers lost_lease, applies nothing');
    // home's lease is _deactivated's: a per-host mask on the home id is not read
    ps.local = { leaseInactive: true };
    a = await call('local', sock('bL'), Object.assign({ rid: 91 }, F(7)));
    ok(a && a.ok === true && countMut('reveal') === 1,
        'H2 ...while the home host still applies');
    a = await call('remoteY', sock('bY'), { rid: 93, op: 'get', args: { visible: null } });
    ok(a && a.ok === true, 'H4 ...and so does another remote host');
    delete ps.remoteX; delete ps.local;
    resetWorld();
    rearmPush(false);
    const wH3 = sock('bR');
    const pH3 = api.handleDesktopRpc('remoteX', { ws: wH3 }, wH3,
        { rid: 92, op: 'float', args: { id: 5, visible: null } });
    await Promise.resolve(); await Promise.resolve();
    ps.remoteX = { leaseInactive: true };
    pushResolve(); await pH3;
    a = wH3.sent[wH3.sent.length - 1];
    ok(a && a.error === 'lost_lease', 'H3 a remote host masked during the flush answers lost_lease');
    rearmPush(true);
    delete ps.remoteX;

    console.log(pass + ' passed, ' + fail + ' failed');
    process.exitCode = fail ? 1 : 0;
})().catch(function (e) {
    console.error(e && e.stack || e);
    process.exitCode = 1;
});
"""

_LIVE_SCENARIOS = r"""
// ---- #250 F: a run-less broker's first status forgets its answers --------
{
    const forgot = [];
    let w = mkWorld({ env: { _desktopForgetRun: function (h) { forgot.push(h); } } });
    w.api.openControlWs(w.env.localHost());
    let s = w.last(); s._open(); s._recv(OLD_ST(true));
    s._recv(OLD_ST(true));
    ok(J(forgot) === J(['local']),
        'F1 a first status without a bootId forgets that host\'s answers (once per socket)');
    forgot.length = 0;
    w = mkWorld({ env: { _desktopForgetRun: function (h) { forgot.push(h); } } });
    connected(w);
    ok(forgot.length === 0, 'F2 a first status that names a run forgets nothing');
}
console.log(pass + ' passed, ' + fail + ' failed');
process.exitCode = fail ? 1 : 0;
"""


def _run(harness_src, cut, scenarios, start_end=None):
    assert harness_src.count(cut) == 1, f"harness cut {cut!r} not unique"
    src = harness_src[:harness_src.index(cut)]
    src = src.replace("__SRC_PATH__", json.dumps(str(rpc.SRC_JS)))
    # #262 review round 1: the rpc world also loads 58's real decideTiled.
    src = src.replace("__SRC58_PATH__", json.dumps(str(rpc.SRC_58)))
    if start_end:
        src = (src.replace("__START__", json.dumps(start_end[0]))
                  .replace("__END__", json.dumps(start_end[1])))
    return src + scenarios


def _exec(tmp_path, name, script, expected):
    path = tmp_path / name
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([rpc.NODE, str(path)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    tail = re.match(r"^(\d+) passed, (\d+) failed$", lines[-1] if lines else "")
    assert tail is not None, (
        f"no machine-readable tail\nrc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}")
    fails = [ln for ln in lines if ln.startswith("FAIL: ")]
    assert proc.returncode == 0 and not fails, (
        f"rc={proc.returncode}\n" + "\n".join(fails)
        + f"\nstdout={proc.stdout}\nstderr={proc.stderr}")
    assert int(tail.group(1)) == expected, (
        f"{tail.group(1)} asserts ran, {expected} expected\n{proc.stdout}")


def test_desktop_rpc_cache_and_lease_scenarios(tmp_path):
    """K (the cache key), D (in-flight duplicates), N/H (lease checks)."""
    _exec(tmp_path, "rpc250.js",
          _run(rpc._HARNESS, _RPC_CUT, _RPC_SCENARIOS),
          EXPECTED_RPC_ASSERTS)


def test_runless_broker_status_forgets_its_answers(tmp_path):
    """F: the /control status path calls the forget for a run-less host."""
    _exec(tmp_path, "live250.js",
          _run(liveness._HARNESS, _LIVE_CUT, _LIVE_SCENARIOS,
               (liveness.START, liveness.END)),
          EXPECTED_LIVE_ASSERTS)
