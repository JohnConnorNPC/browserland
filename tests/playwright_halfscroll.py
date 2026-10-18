"""#259 live gate: focus moving between two 1/2 columns never scrolls the strip.

NOT collected by pytest (no test_ prefix): it needs Playwright's Chromium and
starts its OWN scratch broker, as a child process, on a free port (never
4445/4447/8445/9222), with --no-terminals, from the tree given by --tree (cwd
AND PYTHONPATH, so the page served is that tree's) and every CLAUDE*/ANTHROPIC*
variable scrubbed. The broker is stopped with terminate()+wait() in a finally;
if it outlives that, the script reports its PID and port and leaves it.

    python tests/playwright_halfscroll.py --tree DIR --expect base|fix [--headed]

Two fake producers (the /browserland hello) stand in for shells, so the two
windows are real terminal windows. They are tiled in their own columns and
set to 1/2 through setColumnPreset (the title-bar width control's path).
For each strip width the script clicks left, right, left, right on the
windows and logs every scrollLeft write (an instance setter wrapper) and
every scroll event, with clientWidth, the strip's real width, scrollWidth and
each column's offset/rect widths.

--expect base: a scroll is EXPECTED wherever the columns overrun clientWidth
(the bug reproduces); --expect fix: no width may scroll, and the columns plus
gutter must equal clientWidth. Both: a column wider than the viewport still
comes into view from the taskbar. Each check prints PASS/FAIL; the exit code
is the failure count.
"""
import argparse, asyncio, json, os, secrets, socket, subprocess, sys
import threading, time, urllib.request
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--tree", required=True)
_ap.add_argument("--expect", required=True, choices=("base", "fix"))
_ap.add_argument("--headed", action="store_true")
_args = _ap.parse_args()

TREE = Path(_args.tree).resolve()
FIX = _args.expect == "fix"
TOKEN = secrets.token_hex(16)
WIDS = [259000 + (int(time.time()) % 1000) * 2 + i for i in (0, 1)]
KEYS = ["local:%d" % w for w in WIDS]
FAILS = []
REFUSED_PORTS = {4445, 4447, 8445, 9222}


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        FAILS.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    if p in REFUSED_PORTS:
        sys.exit("refusing reserved port %d" % p)
    return p


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


# ---- the scratch broker (our own child) --------------------------------------
PORT = free_port()
BASE = "http://127.0.0.1:%d" % PORT
env = {k: v for k, v in os.environ.items()
       if not (k.upper().startswith("CLAUDE") or k.upper().startswith("ANTHROPIC"))}
env["WEB_TERMINAL_TOKEN"] = TOKEN
env["PYTHONPATH"] = str(TREE)
env.pop("BROWSERLAND_BROKER_URL", None)
BROKER = subprocess.Popen(
    [sys.executable, "-m", "webterm.broker", "--host", "127.0.0.1",
     "--port", str(PORT), "--no-terminals"],
    cwd=str(TREE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("scratch broker pid %d port %d tree %s" % (BROKER.pid, PORT, TREE), flush=True)


def stop_broker():
    if BROKER.poll() is None:
        BROKER.terminate()
        try:
            BROKER.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("LEAK scratch broker pid %d port %d still running (left alone)"
                  % (BROKER.pid, PORT), flush=True)
            FAILS.append("broker leak")
            return
    print("scratch broker stopped (exit %s); port %d free: %s"
          % (BROKER.returncode, PORT, port_free(PORT)), flush=True)


class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


class Producer:
    """A shell stand-in that answers every resize with `resized`."""
    async def start(self, wid):
        self.ws = await websockets.connect(
            "ws://127.0.0.1:%d/browserland?token=%s" % (PORT, TOKEN), max_size=None)
        await self.ws.send(json.dumps({"type": "hello", "window_id": wid, "pid": 7,
                                       "title": "hs%d" % wid, "cols": 80, "rows": 24}))
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    continue
                f = json.loads(m)
                if f.get("type") == "resize":
                    await self.ws.send(json.dumps({"type": "resized",
                                                   "cols": f["cols"], "rows": f["rows"]}))
        except Exception:
            pass


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


# Wrap the strip's scrollLeft setter on the instance (every JS write is
# logged) and listen for scroll events (native scrolls too).
INSTRUMENT = """() => { const s = document.getElementById('strip');
  if (!s.__hs) { s.__hs = true; window.__w = []; window.__e = [];
    const d = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollLeft');
    window.__setSL = v => d.set.call(s, v);
    Object.defineProperty(s, 'scrollLeft', { configurable: true,
      get() { return d.get.call(this); },
      set(v) { window.__w.push(v); d.set.call(this, v); } });
    s.addEventListener('scroll', () => window.__e.push(d.get.call(s))); }
  window.__w.length = 0; window.__e.length = 0; return true; }"""
MEASURE = """(keys) => { const s = document.getElementById('strip');
  const cols = Array.from(s.querySelectorAll(':scope > .strip-col'));
  const g = s.querySelector(':scope > .strip-col-gutter');
  return { cw: s.clientWidth, rw: s.getBoundingClientRect().width, sw: s.scrollWidth,
    sl: s.scrollLeft, dpr: devicePixelRatio, focusedCol: getLayout().focusedCol,
    cols: cols.map(c => [c.offsetLeft, c.offsetWidth, c.getBoundingClientRect().width]),
    gutter: g ? g.getBoundingClientRect().width : null,
    vis: keys.map(k => { const l = findKeyInLayout(k);
      return l ? visibleColumns().indexOf(l.col) : -1; }),
    writes: (window.__w || []).slice(), events: (window.__e || []).slice() }; }"""


def boot(page):
    page.goto(BASE + "/?token=" + TOKEN)
    ok = wait_for(page, "typeof _booted !== 'undefined' && _booted && _stateReady", 25)
    if page.evaluate("document.getElementById('become-active-overlay').classList.contains('open')"):
        page.click("#become-active-btn")
        page.wait_for_timeout(800)
    return ok


def setup(page):
    """Two terminal windows tiled in their own columns, each set to 1/2."""
    page.evaluate("() => { if (!isTilingMode()) enterTilingMode(); }")
    for k in KEYS:
        wait_for(page, "(k) => sessions.has(k)", 20, k)
        page.evaluate("(k) => { if (!windows.has(k)) openWindow(k, sessions.get(k)); }", k)
        wait_for(page, "(k) => windows.has(k) && windows.get(k).tiled", 10, k)
    page.wait_for_timeout(500)
    for k in KEYS:
        page.evaluate("(k) => setColumnPreset(windows.get(k), '1/2')", k)
    page.wait_for_timeout(600)
    m = page.evaluate(MEASURE, KEYS)
    return m


def click_win(page, k):
    page.locator('.term-window[data-session-id="%s"] .term-body' % k).click()
    page.wait_for_timeout(700)      # > the 350 ms default slide


def focus_run(page, label, expect_scroll):
    page.evaluate("() => requestRelayout()")
    page.wait_for_timeout(600)
    page.evaluate(INSTRUMENT)
    page.evaluate("() => window.__setSL(0)")
    page.wait_for_timeout(200)
    page.evaluate(INSTRUMENT)        # clear the logs after the reset
    m0 = page.evaluate(MEASURE, KEYS)
    col_sum = sum(c[1] for c in m0["cols"]) + (m0["gutter"] or 0) * (len(m0["cols"]) - 1)
    print("  %s: clientWidth %d rect %.3f scrollWidth %d dpr %s cols %s gutter %s sum %s"
          % (label, m0["cw"], m0["rw"], m0["sw"], m0["dpr"], m0["cols"], m0["gutter"],
             col_sum), flush=True)
    seq = []
    for k in (KEYS[0], KEYS[1], KEYS[0], KEYS[1]):
        click_win(page, k)
        m = page.evaluate(MEASURE, KEYS)
        seq.append((m["sl"], m["focusedCol"]))
    m1 = page.evaluate(MEASURE, KEYS)
    vis = m1["vis"]
    focus_ok = [f for _sl, f in seq] == [vis[0], vis[1], vis[0], vis[1]]
    moved = any(sl != m0["sl"] for sl, _f in seq) or bool(m1["events"])
    print("  %s: scrollLeft after clicks %s, writes %s, scroll events %s"
          % (label, [s for s, _f in seq], m1["writes"], m1["events"]), flush=True)
    check(len(m0["cols"]) == 2 and vis[0] != vis[1] and focus_ok,
          "%s: two columns, the clicks moved focus L,R,L,R (real focus path)" % label)
    if expect_scroll is None:
        check(not moved and not m1["writes"],
              "%s: focus never scrolled the strip (no write, no scroll event)" % label)
        check(col_sum == m0["cw"],
              "%s: columns + gutter %s == clientWidth %d" % (label, col_sum, m0["cw"]))
    else:
        check(moved == expect_scroll,
              "%s: base %s (columns+gutter %s vs clientWidth %d)"
              % (label, "scrolls (bug reproduced)" if expect_scroll else "does not scroll",
                 col_sum, m0["cw"]))
    return m0


def wide_run(page):
    """Columns wider than the strip ("viewport narrowed after layout": the
    layout model can't produce one, since getLayout's reconcile drops a
    widthFrac > 1). Two 'max' columns are laid out at 1920, then #desktop is
    narrowed to 1000px without a relayout. Each taskbar click targets the
    window NOT in front (a chip click on the front window minimizes it). A
    wholly off-screen column comes in at its left edge; a partly visible one
    is left alone."""
    for k in KEYS:
        page.evaluate("(k) => setColumnPreset(windows.get(k), 'max')", k)
    page.wait_for_timeout(700)
    click_win(page, KEYS[0])                      # col 0 in front
    page.evaluate("() => { document.getElementById('desktop').style.width = '1000px'; }")
    page.wait_for_timeout(300)
    page.evaluate(INSTRUMENT)
    chip = lambda k: page.locator('.taskbar-item[data-session-id="%s"]' % k)

    def wide_now(m):
        return len(m["cols"]) == 2 and all(c[1] > m["cw"] for c in m["cols"])
    m = page.evaluate(MEASURE, KEYS)
    print("  wide: clientWidth %d cols %s scrollLeft %s" % (m["cw"], m["cols"], m["sl"]),
          flush=True)
    c0, c1 = (m["cols"] + [[0, 0, 0], [0, 0, 0]])[:2]
    check(wide_now(m) and m["sl"] + m["cw"] <= c1[0],
          "wide precondition: both columns wider than the strip, col 1 wholly off "
          "(scrollLeft %s, col 1 starts %d)" % (m["sl"], c1[0]))
    chip(KEYS[1]).click()
    page.wait_for_timeout(900)
    m = page.evaluate(MEASURE, KEYS)
    check(wide_now(m) and m["sl"] == c1[0],
          "wide: wholly off-screen col 1 comes in at its left edge (scrollLeft %s want %d)"
          % (m["sl"], c1[0]))
    check(c0[0] + c0[1] <= m["sl"],
          "wide precondition: col 0 now wholly off (scrollLeft %s, col 0 ends %d)"
          % (m["sl"], c0[0] + c0[1]))
    chip(KEYS[0]).click()
    page.wait_for_timeout(900)
    m = page.evaluate(MEASURE, KEYS)
    check(wide_now(m) and m["sl"] == c0[0],
          "wide: wholly off-screen col 0 comes in at its left edge (scrollLeft %s want %d)"
          % (m["sl"], c0[0]))
    part = c0[0] + c0[1] - 400                   # col 0's last 400px + col 1's first
    page.evaluate("(v) => window.__setSL(v)", part)
    page.wait_for_timeout(200)
    page.evaluate(INSTRUMENT)
    m = page.evaluate(MEASURE, KEYS)
    check(wide_now(m) and m["sl"] == part and part + m["cw"] > c1[0],
          "wide precondition: col 1 partly on screen (scrollLeft %s)" % m["sl"])
    chip(KEYS[1]).click()
    page.wait_for_timeout(900)
    m = page.evaluate(MEASURE, KEYS)
    check(m["sl"] == part and not m["writes"],
          "wide: partly visible col 1 is left alone (scrollLeft %s, writes %s)"
          % (m["sl"], m["writes"]))
    page.evaluate("() => { document.getElementById('desktop').style.width = ''; }")
    for k in KEYS:
        page.evaluate("(k) => setColumnPreset(windows.get(k), '1/2')", k)
    page.wait_for_timeout(600)


# Integer CSS widths via the viewport; fractional ones via an inline width on
# #desktop (#strip is inset:0 inside it), each confirmed non-integer first.
VIEWPORT_CASES = [1919, 1920, 1365]
FRACTIONAL_CASES = [1535.2, 1535.6, 1745.45]


def predicted_overrun(cw):
    # base: an odd width left after the gutter makes both halves k+.5, and
    # Math.round takes both up.
    return (cw - 6) % 2 == 1


try:
    deadline = time.time() + 25
    while True:
        try:
            http(BASE + "/sessions")
            break
        except Exception:
            if BROKER.poll() is not None or time.time() > deadline:
                raise SystemExit("scratch broker did not come up")
            time.sleep(0.2)
    html = http(BASE + "/").decode("utf-8", "replace")
    check(("columnPixelWidths" in html) == FIX,
          "setup: the broker serves the %s build (columnPixelWidths %s in the page)"
          % (_args.expect, "present" if FIX else "absent"))
    BG = Bg()
    PRODS = [Producer(), Producer()]
    for p, w in zip(PRODS, WIDS):
        BG.run(p.start(w))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not _args.headed)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1000})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        check(boot(page), "setup: the desktop booted on the scratch broker")
        m = setup(page)
        check(len(m["cols"]) == 2, "setup: two tiled 1/2 columns (%s)" % m["cols"])
        for w in VIEWPORT_CASES:
            page.set_viewport_size({"width": w, "height": 1000})
            page.wait_for_timeout(600)
            cw = page.evaluate("document.getElementById('strip').clientWidth")
            focus_run(page, "W=%d" % w, None if FIX else predicted_overrun(cw))
        page.set_viewport_size({"width": 1920, "height": 1000})
        for w in FRACTIONAL_CASES:
            page.evaluate("(v) => { document.getElementById('desktop').style.width = v + 'px'; }", w)
            page.wait_for_timeout(300)
            rw = page.evaluate("document.getElementById('strip').getBoundingClientRect().width")
            cw = page.evaluate("document.getElementById('strip').clientWidth")
            frac = abs(rw - round(rw)) > 1e-3
            check(frac, "W=%s: the strip really is fractional (rect %.4f, clientWidth %d)"
                  % (w, rw, cw))
            if frac:
                focus_run(page, "W=%s" % w, None if FIX else predicted_overrun(cw))
            else:
                print("  W=%s UNMEASURED" % w, flush=True)
        page.evaluate("() => { document.getElementById('desktop').style.width = ''; }")
        page.wait_for_timeout(400)
        wide_run(page)
        check(not errors, "no page errors (%s)" % errors[:3])
        browser.close()

        # The DSF route (amendment: try it first-hand, informational): a
        # 1920-device-px window at 1.1 scale is a 1745.45 CSS px viewport if
        # Chrome lays it out fractionally.
        b2 = pw.chromium.launch(headless=not _args.headed,
                                args=["--force-device-scale-factor=1.1",
                                      "--window-size=1920,1000"])
        c2 = b2.new_context(no_viewport=True)
        p2 = c2.new_page()
        if boot(p2):
            setup(p2)
            rw = p2.evaluate("document.getElementById('strip').getBoundingClientRect().width")
            cw = p2.evaluate("document.getElementById('strip').clientWidth")
            dpr = p2.evaluate("devicePixelRatio")
            print("  DSF route: dpr %s strip rect %.4f clientWidth %d" % (dpr, rw, cw), flush=True)
            if abs(rw - round(rw)) > 1e-3 or cw % 2 == 1:
                focus_run(p2, "DSF1.1 W=%.2f" % rw, None if FIX else predicted_overrun(cw))
            else:
                print("  DSF route gave an even integer strip; the inline-width cases stand in",
                      flush=True)
        else:
            print("  DSF route: page did not boot (informational)", flush=True)
        b2.close()
finally:
    stop_broker()

print("RESULT %s: %d failure(s)" % (_args.expect, len(FAILS)), flush=True)
sys.exit(len(FAILS))
