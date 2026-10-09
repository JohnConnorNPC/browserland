"""#239 acceptance: a terminal controlled from another device, end to end.

NOT collected by pytest (no test_ prefix): it needs Playwright's Chromium and
starts its OWN scratch broker on a free port, from a throwaway cwd with
PYTHONPATH at this repo and every CLAUDE*/ANTHROPIC* variable scrubbed, so the
page under test is always the tree on disk (a broker caches its page at
import). The broker it started is stopped by PID at the end; nothing else is.

    python tests/playwright_control.py [--headed] [--keep-workdir]

A fake producer (the /browserland hello) stands in for a shell, a scripted
controller socket (/ws, clientId ext-ctl, never /control) claims and releases,
and a scripted /control client `B` takes the lease when a check needs another
browser. Every page socket goes through Playwright's route_web_socket: an
in-path recording proxy that logs what the page SENT on each /ws and /control
socket and can drop or inject what it RECEIVES. /sessions polls can be stalled
(aborted) to prove a change arrived another way. Each check prints PASS/FAIL
with its id (P1..P18, matching 239/atoms.md); the exit code is the failures.
"""
import argparse, asyncio, json, os, re, secrets, shutil, socket, subprocess
import sys, tempfile, threading, time, urllib.request
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
_ap = argparse.ArgumentParser()
_ap.add_argument("--headed", action="store_true")
_ap.add_argument("--keep-workdir", action="store_true")
_args = _ap.parse_args()

TOKEN = secrets.token_hex(16)
WID = 239000 + (int(time.time()) % 1000)
KEY = "local:%d" % WID
FAILS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        FAILS.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


# ---- the scratch broker -----------------------------------------------------
PORT = free_port()
BASE = "http://127.0.0.1:%d" % PORT
WORKDIR = Path(tempfile.mkdtemp(prefix="bl239-"))
env = {k: v for k, v in os.environ.items()
       if not (k.upper().startswith("CLAUDE") or k.upper().startswith("ANTHROPIC"))}
env["WEB_TERMINAL_TOKEN"] = TOKEN
env["PYTHONPATH"] = str(REPO)
env.pop("BROWSERLAND_BROKER_URL", None)
BROKER = subprocess.Popen(
    [sys.executable, "-m", "webterm.broker", "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=str(WORKDIR), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("scratch broker pid %d port %d" % (BROKER.pid, PORT), flush=True)


def stop_broker():
    if BROKER.poll() is None:
        BROKER.terminate()
        try:
            BROKER.wait(timeout=8)
        except subprocess.TimeoutExpired:
            BROKER.kill()
    if not _args.keep_workdir:
        shutil.rmtree(WORKDIR, ignore_errors=True)


deadline = time.time() + 25
while True:
    try:
        http_json(BASE + "/sessions")
        break
    except Exception:
        if BROKER.poll() is not None or time.time() > deadline:
            stop_broker()
            sys.exit("scratch broker did not come up")
        time.sleep(0.2)
info = http_json(BASE + "/info")
if info.get("terminal_control") != 1:
    stop_broker()
    sys.exit("scratch broker does not advertise terminal_control: 1")


# ---- background sockets (fake producer, controller, lease client B) ---------
class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


BG = Bg()
WS_BASE = "ws://127.0.0.1:%d" % PORT


class Producer:
    """A shell stand-in: logs every frame the broker forwards, echoes each
    resize as `resized` (what a real agent does)."""
    def __init__(self):
        self.frames = []

    async def start(self, wid):
        self.ws = await websockets.connect(
            WS_BASE + "/browserland?token=" + TOKEN, max_size=None)
        await self.ws.send(json.dumps({"type": "hello", "window_id": wid, "pid": 7,
                                       "title": "ctl239", "cols": 80, "rows": 24}))
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    continue
                f = json.loads(m)
                self.frames.append(f)
                if f.get("type") == "resize":
                    await self.ws.send(json.dumps({"type": "resized",
                                                   "cols": f["cols"], "rows": f["rows"]}))
        except Exception:
            pass

    async def out(self, data):
        await self.ws.send(data)

    async def close(self):
        await self.ws.close()

    def count(self, kind):
        return sum(1 for f in self.frames if f.get("type") == kind)


class Controller:
    def __init__(self, wid):
        self.wid = wid
        self.frames = []
        self.ws = None

    async def open(self):
        self.frames = []
        self.ws = await websockets.connect(
            WS_BASE + "/ws?session=%d&token=%s&clientId=ext-ctl" % (self.wid, TOKEN),
            max_size=None)
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            async for m in self.ws:
                if not isinstance(m, bytes):
                    self.frames.append(json.loads(m))
        except Exception:
            pass

    async def claim(self, label):
        await self.ws.send(json.dumps({"type": "control", "op": "claim", "label": label}))

    async def release(self):
        await self.ws.send(json.dumps({"type": "control", "op": "release"}))

    async def resize(self, cols, rows):
        await self.ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    async def close(self):
        await self.ws.close()


class LeaseClient:
    async def open(self, cid):
        self.ws = await websockets.connect(
            WS_BASE + "/control?clientId=%s&token=%s" % (cid, TOKEN))

    async def become_active(self):
        await self.ws.send(json.dumps({"type": "become_active"}))


def live_controller():
    for s in http_json(BASE + "/sessions"):
        if s["id"] == WID:
            return s.get("controller")
    return "gone"


# ---- the recording proxy ------------------------------------------------------
class Proxy:
    def __init__(self):
        self.ws_opens = []            # (t, url) of every page /ws dial to WID
        self.ws_closed = {}           # socket# -> t the PAGE closed it
        self.ws_count = 0
        self.ws_sent = []             # (t, socket#, frame) page -> broker on /ws
        self.ws_recv = []             # (t, socket#, frame) broker -> page on /ws
        self.control_routes = []
        self.ws_nums = []             # socket# of each page /ws dial to WID
        self.drop_terminal_control = False
        self.drop_status_inactive = False
        self.stall_polls = False

    def on_ws(self, route):
        n = self.ws_count
        self.ws_count += 1
        mine = ("session=%d" % WID) in route.url
        if mine:
            self.ws_opens.append((time.time(), route.url))
            self.ws_nums.append(n)
        server = route.connect_to_server()

        # The WIRE fact "the page closed this socket": the route's own
        # `closePage` event (a close from the page side). Listened to beside
        # the default forwarding rather than through route.on_close, whose
        # Python handler raises KeyError('code') on a close without a code in
        # Playwright 1.57 -- which is exactly the close a page's ws.close()
        # sends.
        route._impl_obj._channel.on(
            "closePage", lambda _ev: self.ws_closed.setdefault(n, time.time()))

        def up(m):
            if mine and isinstance(m, str):
                try:
                    self.ws_sent.append((time.time(), n, json.loads(m)))
                except ValueError:
                    pass
            server.send(m)

        def down(m):
            if mine and isinstance(m, str):
                try:
                    self.ws_recv.append((time.time(), n, json.loads(m)))
                except ValueError:
                    pass
            route.send(m)
        route.on_message(up)
        server.on_message(down)

    def on_control(self, route):
        server = route.connect_to_server()
        self.control_routes.append(route)

        def down(m):
            try:
                f = json.loads(m) if isinstance(m, str) else None
            except ValueError:
                f = None
            if f and f.get("type") == "terminal_control" and self.drop_terminal_control:
                return
            if (f and f.get("type") == "status" and f.get("active") is False
                    and self.drop_status_inactive):
                return
            route.send(m)
        route.on_message(lambda m: server.send(m))
        server.on_message(down)

    def on_poll(self, route):
        if self.stall_polls:
            route.abort()
        else:
            route.continue_()

    def resizes_since(self, t):
        return [f for (ts, _n, f) in self.ws_sent if ts >= t and f.get("type") == "resize"]

    def opens_since(self, t):
        return [u for (ts, u) in self.ws_opens if ts >= t]

    def last_socket_before(self, t):
        nums = [n for (ts, _u), n in zip(self.ws_opens, self.ws_nums) if ts < t]
        return nums[-1] if nums else None

    def sockets_since(self, t):
        return [n for (ts, _u), n in zip(self.ws_opens, self.ws_nums) if ts >= t]

    def closed_by_page_within(self, n, t, secs):
        c = self.ws_closed.get(n)
        return c is not None and t - 0.2 <= c <= t + secs


PROXY = Proxy()


# ---- page helpers --------------------------------------------------------------
WIN = """(key) => { const w = windows.get(key); if (!w) return null;
  const chip = document.querySelector('.taskbar-item[data-session-id="' + CSS.escape(key) + '"]');
  const r = w.dom.getBoundingClientRect();
  return {
    controlled: w.controlled ? {label: w.controlled.label, gen: w.controlled.gen,
                                frozen: Object.isFrozen(w.controlled)} : null,
    hasWs: !!w.ws, wsOpen: !!w.wsOpen, grey: w.dom.classList.contains('controlled'),
    overlay: !!(w.ctlOverlay && w.ctlOverlay.isConnected),
    labelText: w.ctlLabel ? w.ctlLabel.textContent : null,
    labelChildren: w.ctlLabel ? w.ctlLabel.children.length : null,
    detached: !!w.detached, minimized: !!w.minimized, tiled: !!w.tiled,
    loc: (typeof findKeyInLayout === 'function') ? JSON.stringify(findKeyInLayout(key)) : null,
    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    cols: w.term.cols, rows: w.term.rows,
    chip: !!chip, chipControlled: !!(chip && chip.classList.contains('controlled')),
    chipTitle: chip ? chip.title : '',
    focusBtn: !!(w.ctlBtn && document.activeElement === w.ctlBtn),
    rec: terminalControl[key] ? Object.assign({}, terminalControl[key]) : null,
  }; }"""
BUF_HAS = """(a) => { const w = windows.get(a[0]); const b = w.term.buffer.active;
  for (let i = 0; i < b.length; i++) if (b.getLine(i).translateToString(true).includes(a[1])) return true;
  return false; }"""
EXPECTED_GRID = """(key) => { const w = windows.get(key); const d = readCellDims(w);
  const r = w.body.getBoundingClientRect();
  return d ? [Math.floor((r.width - 8) / d.w), Math.floor((r.height - 8) / d.h)] : null; }"""
HOOKS = """() => { if (!window.__hooks) { window.__hooks = [];
  const orig = controlStateChanged;
  controlStateChanged = function (k) { window.__hooks.push(String(k)); return orig(k); }; }
  return window.__hooks.length; }"""


def wait_for(page, js, timeout=15, arg=None):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if page.evaluate(js, arg) if arg is not None else page.evaluate(js):
                return True
        except Exception:
            pass
        page.wait_for_timeout(150)
    return False


def win(page):
    return page.evaluate(WIN, KEY)


def hooks(page):
    return page.evaluate(HOOKS)


CONTROLLED = "(k) => { const w = windows.get(k); return !!(w && w.controlled); }"
FREE_ATTACHED = "(k) => { const w = windows.get(k); return !!(w && !w.controlled && w.wsOpen); }"

PAGE_ERRORS = []


def boot(page):
    page.goto(BASE + "/?token=" + TOKEN)
    ok = wait_for(page, "typeof _booted !== 'undefined' && _booted && _stateReady", 25)
    page.evaluate(HOOKS)
    return ok


def open_window(page):
    wait_for(page, "(k) => sessions.has(k)", 20, KEY)
    page.evaluate("(k) => { if (!windows.has(k)) openWindow(k, sessions.get(k)); }", KEY)
    return wait_for(page, "(k) => windows.has(k)", 10, KEY)


PROD = Producer()
CTL = Controller(WID)
B = LeaseClient()

try:
    BG.run(PROD.start(WID))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not _args.headed)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.route_web_socket(re.compile(r"/ws\?"), PROXY.on_ws)
        ctx.route_web_socket(re.compile(r"/control\?"), PROXY.on_control)
        ctx.route(re.compile(r"/sessions(\?|$)"), PROXY.on_poll)
        page = ctx.new_page()
        page.on("pageerror", lambda e: PAGE_ERRORS.append(str(e)))
        page.on("console", lambda m: print("  [console]", m.type, m.text)
                if m.type == "error" else None)
        check(boot(page), "setup: the desktop booted on the scratch broker")
        check(not page.evaluate("document.getElementById('become-active-overlay').classList.contains('open')"),
              "setup: this page holds the lease (no Become active overlay)")
        # Floating, so a `resized` snap would be visible on the window box.
        page.evaluate("() => { if (isTilingMode()) enterFloatingMode(); }")
        check(open_window(page) and wait_for(page, FREE_ATTACHED, 15, KEY),
              "setup: the terminal window opened and attached")
        page.wait_for_timeout(800)
        BG.run(CTL.open())

        # ---- P1 claim keeps the window in place, closes its socket ----------
        before = win(page)
        h0 = hooks(page)
        label = "<b>x</b> phone"
        t_claim = time.time()
        BG.run(CTL.claim(label))
        ok = wait_for(page, CONTROLLED, 10, KEY)
        st = win(page)
        check(ok and st["controlled"]["label"] == label and st["controlled"]["frozen"],
              "P1 claim: win.controlled = frozen {label, since, gen}")
        page.wait_for_timeout(300)
        n1 = PROXY.last_socket_before(t_claim)
        check(not st["hasWs"] and not st["wsOpen"] and n1 is not None
              and PROXY.closed_by_page_within(n1, t_claim, 2.0),
              "P1 claim: the window holds no socket (the page CLOSED its /ws on the wire)")
        check(st["labelText"] == label and st["labelChildren"] == 0,
              "P1 claim: the overlay shows the label as TEXT (markup not parsed)")
        check(st["grey"] and st["overlay"], "P1 claim: greyed with the overlay")
        check(st["rect"] == before["rect"] and st["loc"] == before["loc"] and st["chip"],
              "P1 claim: same box, same layout slot, chip kept")
        check(hooks(page) - h0 == 1, "P1 claim: exactly one controlStateChanged")
        # Both re-dial loops (75's heal tick, 63's auth success) reach a
        # socketless window; guard G is what refuses them.
        page.wait_for_timeout(4500)
        check(PROXY.opens_since(t_claim) == [] and win(page)["controlled"],
              "P1 claim: no re-dial while controlled across two heal ticks")
        BG.run(PROD.out(b"CTLMARK_239\r\n"))
        page.wait_for_timeout(1200)
        check(not page.evaluate(BUF_HAS, [KEY, "CTLMARK_239"]),
              "P1 claim: the controller-era output never reached the page's xterm")
        check(page.evaluate("() => { const ov = document.querySelector('.term-ctl-overlay'); "
                            "return !!ov && Number(getComputedStyle(ov).zIndex) === 20; }"),
              "P1 claim: the overlay declares z-index 20 (a declaration pin, not a stacking test)")

        # ---- P12 chip marked, survives a rebuild ----------------------------
        check(st["chipControlled"] and label in st["chipTitle"],
              "P12 chip: .controlled and the tooltip names the controller")
        page.evaluate("(k) => document.querySelector('.taskbar-item[data-session-id=\"' + CSS.escape(k) + '\"]').remove()", KEY)
        ok = wait_for(page, "(k) => { const c = document.querySelector('.taskbar-item[data-session-id=\"' + CSS.escape(k) + '\"]'); return !!(c && c.classList.contains('controlled')); }", 8, KEY)
        check(ok, "P12 chip: a chip the poll rebuilt is marked .controlled again")

        # ---- P11 focus lands on Take back; keys go nowhere ------------------
        page.evaluate("(k) => bringToFront(k)", KEY)
        page.wait_for_timeout(300)
        check(win(page)["focusBtn"], "P11 focus: bringToFront focuses Take back control")
        n_in = PROD.count("input")
        page.keyboard.type("zz")
        page.wait_for_timeout(600)
        check(PROD.count("input") == n_in and not PAGE_ERRORS,
              "P11 focus: a keystroke while controlled reaches nothing and throws nothing")

        # ---- P2 no resize while controlled ----------------------------------
        t2 = time.time()
        n_res = PROD.count("resize")
        page.evaluate("() => enterTilingMode()")
        page.wait_for_timeout(500)
        page.evaluate("() => enterFloatingMode()")
        page.wait_for_timeout(500)
        page.set_viewport_size({"width": 1200, "height": 800})
        page.wait_for_timeout(500)
        box = page.locator(".term-window[data-session-id=\"%s\"] .rh-se" % KEY).bounding_box()
        if box:
            page.mouse.move(box["x"] + 3, box["y"] + 3)
            page.mouse.down()
            page.mouse.move(box["x"] - 120, box["y"] - 80, steps=6)
            page.mouse.up()
        page.set_viewport_size({"width": 1400, "height": 900})
        page.wait_for_timeout(1500)
        check(box is not None, "P2 setup: the float's resize handle was dragged")
        check(PROXY.resizes_since(t2) == [] and PROXY.opens_since(t2) == []
              and PROD.count("resize") == n_res,
              "P2 no resize: tiling, float drag and browser resizes sent 0 resize frames "
              "(no socket: guard G + the close; isResizable itself is pinned by the collected harness)")

        # ---- P7 real 409 stale_gen relabels -------------------------------------
        PROXY.drop_terminal_control = True
        PROXY.stall_polls = True
        page.wait_for_timeout(300)
        BG.run(CTL.claim("tablet"))          # a relabel: new gen, page not told
        page.wait_for_timeout(800)
        live = live_controller()
        check(win(page)["controlled"]["label"] == label, "P7 setup: the page still shows the old label")
        page.click(".term-window[data-session-id=\"%s\"] .term-ctl-take" % KEY)
        ok = wait_for(page, "(k) => { const w = windows.get(k); return !!(w && w.controlled && w.controlled.label === 'tablet'); }", 8, KEY)
        st = win(page)
        check(ok and st["controlled"]["gen"] == live["gen"],
              "P7 stale_gen: the real 409 relabelled the overlay to the live controller")
        check(st["labelText"] == "tablet" and live_controller() is not None,
              "P7 stale_gen: nothing was released")
        PROXY.drop_terminal_control = False
        PROXY.stall_polls = False

        # ---- P3 take back ----------------------------------------------------------
        page.wait_for_timeout(500)
        live = live_controller()
        posts = []
        page.on("request", lambda r: posts.append(r.post_data_json)
                if r.url.endswith("/session/control") and r.method == "POST" else None)
        # R2: the controller sets its own grid first, so the attach `resized`
        # after Take back carries 50x15; the window must keep its own box.
        BG.run(CTL.resize(50, 15))
        page.wait_for_timeout(600)
        check(any(f.get("type") == "resize" and (f["cols"], f["rows"]) == (50, 15)
                  for f in PROD.frames), "P3 setup: the controller resized the PTY to 50x15")
        # Observed by EVENTS, not by frames: a leave that applied the
        # controller's grid would snap and snap back within one round trip,
        # which per-frame sampling can miss. xterm's own onResize fires
        # synchronously on every grid change; the MutationObserver keeps
        # every intermediate style (attributeOldValue), so no change can
        # hide between two samples. Width/height only: a click also rewrites
        # style.zIndex, which is not a move.
        geo0 = page.evaluate("""(k) => { const w = windows.get(k);
            window.__grids = [];
            window.__gridSub = w.term.onResize((e) => window.__grids.push(e.cols + 'x' + e.rows));
            const box = (st) => { const m = /width:\\s*([^;]+);.*height:\\s*([^;]+);/.exec(st || '');
                                  return m ? m[1] + 'x' + m[2] : String(st); };
            window.__boxes = [w.dom.style.width + 'x' + w.dom.style.height];
            window.__boxObs = new MutationObserver((recs) => {
                for (const r of recs) window.__boxes.push(box(r.oldValue));
                window.__boxes.push(w.dom.style.width + 'x' + w.dom.style.height);
            });
            window.__boxObs.observe(w.dom, {attributes: true, attributeFilter: ['style'],
                                            attributeOldValue: true});
            const blob = JSON.parse(localStorage.getItem('webterm:prefs:v1') || '{}');
            return {style: [w.dom.style.width, w.dom.style.height],
                    pref: JSON.stringify(getPref(k).geom || null),
                    stored: JSON.stringify((blob[k] || {}).geom || null)}; }""", KEY)
        t3 = time.time()
        h0 = hooks(page)
        # Polls stalled: 75's heal loop only re-dials a host whose poll
        # answered, so the one attach below can only be leaveControlled's.
        PROXY.stall_polls = True
        page.wait_for_timeout(2500)
        # The controller's 50x15 is the LAST size the broker holds right
        # before Take back, so the leave's attach `resized` carries it.
        held = [s_ for s_ in http_json(BASE + "/sessions") if s_["id"] == WID]
        check(bool(held) and (held[0]["cols"], held[0]["rows"]) == (50, 15),
              "P3 setup: the broker holds the controller's 50x15 right before Take back")
        page.click(".term-window[data-session-id=\"%s\"] .term-ctl-take" % KEY)
        ok = wait_for(page, FREE_ATTACHED, 10, KEY)
        page.wait_for_timeout(1500)
        PROXY.stall_polls = False
        n3 = PROXY.sockets_since(t3)
        att = [f for (ts, n, f) in PROXY.ws_recv if n3 and n == n3[0] and f.get("type") == "resized"]
        check(bool(att) and (att[0]["cols"], att[0]["rows"]) == (50, 15),
              "P3 setup: the leave's attach `resized` carried the controller's 50x15")
        geo1 = page.evaluate("""(k) => { const w = windows.get(k);
            const blob = JSON.parse(localStorage.getItem('webterm:prefs:v1') || '{}');
            return {style: [w.dom.style.width, w.dom.style.height],
                    pref: JSON.stringify(getPref(k).geom || null),
                    stored: JSON.stringify((blob[k] || {}).geom || null),
                    boxes: Array.from(new Set(window.__boxes)), grids: window.__grids.slice(),
                    _off: (window.__gridSub.dispose(), window.__boxObs.disconnect(), 0)}; }""", KEY)
        check(geo1["style"] == geo0["style"]
              and geo1["boxes"] == ["%sx%s" % tuple(geo0["style"])],
              "P3 take back (R2): the float's box never moved toward the controller's grid %s"
              % (geo1["boxes"],))
        check(geo1["pref"] == geo0["pref"] and geo1["stored"] == geo0["stored"],
              "P3 take back (R2): the saved geometry (prefs + localStorage) is unchanged")
        check("50x15" not in geo1["grids"],
              "P3 take back (R2): the xterm never took the controller's 50x15 (grids seen: %s)"
              % (geo1["grids"],))
        cid = page.evaluate("CLIENT_ID")
        check(ok and posts == [{"id": str(WID), "op": "release", "gen": live["gen"], "clientId": cid}],
              "P3 take back: one POST with the live gen and this browser's clientId")
        check(len(PROXY.opens_since(t3)) == 1, "P3 take back: exactly one new /ws attach")
        want = page.evaluate(EXPECTED_GRID, KEY)
        res = PROXY.resizes_since(t3)
        check(bool(res) and [res[0]["cols"], res[0]["rows"]] == want,
              "P3 take back: the first resize on it is the current box's grid %s" % (want,))
        check(any(f.get("type") == "resize" and [f["cols"], f["rows"]] == want
                  for f in PROD.frames[-6:]),
              "P3 take back: the producer received the page's cols/rows")
        check(hooks(page) - h0 == 1, "P3 take back: exactly one controlStateChanged")
        check(any(f.get("state") == "revoked" and f.get("by") == "browser" for f in CTL.frames),
              "P3 take back: the controller was told revoked by:browser")
        check(win(page)["chipControlled"] is False, "P3 take back: the chip is unmarked")

        # ---- P10 the /ws frame alone greys an attached window ---------------
        PROXY.drop_terminal_control = True
        PROXY.stall_polls = True
        page.wait_for_timeout(300)
        t10 = time.time()
        BG.run(CTL.claim("phone"))
        ok = wait_for(page, CONTROLLED, 3, KEY)
        page.wait_for_timeout(300)
        n10 = PROXY.last_socket_before(t10)
        check(ok and n10 is not None and PROXY.closed_by_page_within(n10, t10, 3.0),
              "P10 /ws frame: greyed with pushes dropped and polls stalled, its /ws closed on the wire")
        PROXY.drop_terminal_control = False

        # ---- P4 controller-side release, push alone ----------------------------
        BG.run(CTL.release())
        check(wait_for(page, FREE_ATTACHED, 4, KEY),
              "P4 push: a controller release frame left the state with polls stalled")
        BG.run(CTL.claim("phone"))
        wait_for(page, CONTROLLED, 4, KEY)
        BG.run(CTL.close())
        check(wait_for(page, FREE_ATTACHED, 4, KEY),
              "P4 push: the controller's socket closing left the state with polls stalled")
        PROXY.stall_polls = False
        BG.run(CTL.open())

        # ---- P9 a forged push heals through the poll; baseline not poisoned ------
        page.wait_for_timeout(800)
        g = page.evaluate("(k) => terminalControl[k] && terminalControl[k].gen", KEY)
        t9 = time.time()
        for r in PROXY.control_routes[-1:]:
            r.send(json.dumps({"type": "terminal_control", "id": WID, "state": "controlled",
                               "label": "forged", "since": int(time.time() * 1000),
                               "gen": (g or 0) + 1000}))
        check(wait_for(page, CONTROLLED, 3, KEY), "P9 forged: an injected push greys the window")
        check(wait_for(page, FREE_ATTACHED, 8, KEY),
              "P9 forged: the poll (controller null) left the state")
        page.wait_for_timeout(500)
        frames = [f for (ts, n, f) in PROXY.ws_recv if ts >= t9 and f.get("type") == "control"]
        check(len(PROXY.opens_since(t9)) == 1 and frames and frames[0].get("state") == "free",
              "P9 forged: one reattach, whose attach frame said free")
        PROXY.stall_polls = True
        PROXY.drop_terminal_control = False
        BG.run(CTL.claim("phone"))
        check(wait_for(page, CONTROLLED, 4, KEY),
              "P9 forged: a real (lower-gen) claim is still taken afterwards")
        PROXY.stall_polls = False
        BG.run(CTL.release())
        wait_for(page, FREE_ATTACHED, 8, KEY)

        # ---- P17 a /control reconnect re-baselines the host's gens --------------
        n_routes = len(PROXY.control_routes)
        PROXY.control_routes[-1].close(code=1012)
        end = time.time() + 12
        while len(PROXY.control_routes) == n_routes and time.time() < end:
            page.wait_for_timeout(200)
        page.wait_for_timeout(800)
        check(len(PROXY.control_routes) > n_routes, "P17 setup: /control re-dialled after a 1012 close")
        g = page.evaluate("(k) => { const r = terminalControl[k]; return r ? r.unordered === true : 'none'; }", KEY)
        check(g is True, "P17 reconnect: that host's recorded gen was forgotten (%r)" % (g,))
        PROXY.stall_polls = True
        PROXY.control_routes[-1].send(json.dumps({
            "type": "terminal_control", "id": WID, "state": "controlled",
            "label": "rebased", "since": int(time.time() * 1000), "gen": 1}))
        check(wait_for(page, "(k) => { const w = windows.get(k); return !!(w && w.controlled && w.controlled.label === 'rebased'); }", 3, KEY),
              "P17 reconnect: a push with a far LOWER gen is accepted after it")
        PROXY.stall_polls = False
        check(wait_for(page, FREE_ATTACHED, 8, KEY), "P17 reconnect: the poll healed it back to free")

        # ---- P16 attach to a terminal the page wrongly thinks is free -----------
        page.evaluate("(k) => closeWindow(k)", KEY)
        page.wait_for_timeout(500)
        PROXY.drop_terminal_control = True
        PROXY.stall_polls = True
        BG.run(CTL.claim("phone"))
        BG.run(CTL.resize(50, 15))
        page.wait_for_timeout(800)
        t16 = time.time()
        n_prod16 = PROD.count("resize")
        geo = page.evaluate("""(k) => { const w = openWindow(k, sessions.get(k));
            const r = w.dom.getBoundingClientRect();
            return [Math.round(r.width), Math.round(r.height), w.term.cols, w.term.rows]; }""", KEY)
        ok = wait_for(page, CONTROLLED, 6, KEY)
        st = win(page)
        check(ok and len(PROXY.opens_since(t16)) == 1,
              "P16 stale attach: greyed by the attach frame of its one dial")
        check([st["rect"][2], st["rect"][3], st["cols"], st["rows"]] == geo
              and (st["cols"], st["rows"]) != (50, 15),
              "P16 stale attach: the held `resized` (50x15) neither resized the xterm nor snapped the float")
        page.wait_for_timeout(1000)
        n16 = PROXY.sockets_since(t16)
        check(len(n16) == 1 and PROXY.closed_by_page_within(n16[0], t16, 6.0),
              "P16 stale attach: the page CLOSED that one socket on the wire")
        check(PROD.count("resize") == n_prod16,
              "P16 stale attach: the producer received NO resize from it (the broker gate, M2); "
              "page resize frames on the wire: %d" % len(PROXY.resizes_since(t16)))
        PROXY.drop_terminal_control = False
        PROXY.stall_polls = False

        # ---- P13 Close does not release; the chip stays marked -----------------
        # Sampled from the close on, every 100 ms for 2.5 s: a record pruned
        # by the close would unmark the chip until the next poll re-marks it.
        marks = page.evaluate("""async (k) => { closeWindow(k); const out = [];
            for (let i = 0; i < 25; i++) {
                const c = document.querySelector('.taskbar-item[data-session-id="' + CSS.escape(k) + '"]');
                out.push(!!(c && c.classList.contains('controlled')));
                await new Promise(r => setTimeout(r, 100)); }
            return out; }""", KEY)
        check(live_controller() not in (None, "gone"), "P13 close: closing the window released nothing")
        check(all(marks), "P13 close: the chip (no window) stays .controlled from the close on (%d/25)" % sum(marks))

        # ---- P5 reload mid-control: re-enter from GET /sessions, never attach ------
        open_window(page)
        wait_for(page, CONTROLLED, 6, KEY)
        # The restore set (openTerms) reaches /state on a 600 ms debounce.
        wait_for(page, "(k) => !_statePushTimer && !_statePushInFlight", 6, KEY)
        page.wait_for_timeout(1500)
        page.reload()
        t5 = time.time()
        boot(page)
        ok = wait_for(page, CONTROLLED, 15, KEY)
        page.wait_for_timeout(6500)                     # three poll cycles
        st = win(page)
        check(ok and st and st["controlled"] and st["overlay"],
              "P5 reload: the window came back greyed from GET /sessions")
        check(PROXY.opens_since(t5) == [], "P5 reload: and dialled no /ws at all")

        # ---- P6 lease flip + rebuild, the claim known only to the poll -----------
        BG.run(CTL.release())
        wait_for(page, FREE_ATTACHED, 8, KEY)
        BG.run(B.open("B"))
        BG.run(B.become_active())
        ok = wait_for(page, "() => _deactivated === true && document.getElementById('become-active-overlay').classList.contains('open')", 8)
        check(ok, "P6 setup: another browser took the lease; the view is torn down")
        PROXY.drop_terminal_control = True
        BG.run(CTL.claim("phone"))
        page.wait_for_timeout(800)
        t6 = time.time()
        page.click("#become-active-btn")
        ok = wait_for(page, CONTROLLED, 15, KEY)
        page.wait_for_timeout(3000)
        check(ok and PROXY.opens_since(t6) == [],
              "P6 rebuild: the window re-entered the state from the poll, 0 /ws dials")
        PROXY.drop_terminal_control = False

        # ---- P8 a real not_active: another browser holds the lease --------------
        PROXY.drop_status_inactive = True
        BG.run(B.become_active())
        page.wait_for_timeout(1000)
        check(not page.evaluate("_deactivated"), "P8 setup: the page has not heard it lost the lease")
        page.click(".term-window[data-session-id=\"%s\"] .term-ctl-take" % KEY)
        ok = wait_for(page, "() => document.getElementById('become-active-overlay').classList.contains('open')", 6)
        check(ok and win(page)["controlled"], "P8 not_active: the Become active overlay is shown; still controlled")
        PROXY.drop_status_inactive = False
        page.click("#become-active-btn")
        ok = wait_for(page, "() => !document.getElementById('become-active-overlay').classList.contains('open')", 6)
        check(ok and not page.evaluate("_deactivated") and win(page)["controlled"],
              "P8 not_active: Become active took the lease back and the overlay came down over the live view")

        # ---- P15 / P14 the detached child -----------------------------------------
        with ctx.expect_page() as pi:
            page.evaluate("(k) => detachWindow(k)", KEY)
        child = pi.value
        child_opens = []
        child.on("websocket", lambda w: child_opens.append(w.url) if ("session=%d" % WID) in w.url else None)
        child.on("pageerror", lambda e: PAGE_ERRORS.append("child: " + str(e)))
        wait_for(child, "typeof _booted !== 'undefined' && _booted", 20)
        ok = wait_for(child, CONTROLLED, 15, KEY)
        child.wait_for_timeout(2500)
        cst = child.evaluate(WIN, KEY)
        check(ok and cst and cst["overlay"] and child_opens == [],
              "P15 child: a popped-out window opened while controlled is greyed and never dialled")
        desk_opens = []
        page.on("websocket", lambda w: desk_opens.append(w.url) if ("session=%d" % WID) in w.url else None)
        BG.run(CTL.release())
        ok = wait_for(child, FREE_ATTACHED, 10, KEY)
        page.wait_for_timeout(2500)
        dst = win(page)
        check(ok and len(child_opens) == 1, "P14 child: on release the child re-attached")
        check(dst["detached"] and not dst["hasWs"] and desk_opens == [],
              "P14 child: the desktop stayed detached and dialled nothing")
        child.close()
        wait_for(page, FREE_ATTACHED, 15, KEY)

        # ---- P18 the record goes with the session ---------------------------------
        BG.run(CTL.claim("phone"))
        wait_for(page, CONTROLLED, 6, KEY)
        page.evaluate("(k) => closeWindow(k)", KEY)
        BG.run(PROD.close())
        ok = wait_for(page, "(k) => !(k in terminalControl) && !document.querySelector('.taskbar-item[data-session-id=\"' + CSS.escape(k) + '\"]')", 20, KEY)
        check(ok, "P18 prune: the producer gone, its chip and control record are dropped")

        check(not PAGE_ERRORS, "no page errors (%s)" % "; ".join(PAGE_ERRORS[:3]))
        browser.close()
finally:
    stop_broker()

print("\nFAILURES:", len(FAILS))
for f in FAILS:
    print(" -", f)
sys.exit(1 if FAILS else 0)
