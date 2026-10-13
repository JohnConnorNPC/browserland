"""#249 acceptance: the page lays out at a phone's real width, and a desktop
browser is untouched by the phone rules.

NOT collected by pytest (no test_ prefix): it needs Playwright's Chromium and
starts its OWN scratch broker on a free port from a throwaway cwd, with
PYTHONPATH at this repo and every CLAUDE*/ANTHROPIC* variable scrubbed, so the
page under test is always the tree on disk (a broker caches its page at
import). Only the broker it started is stopped, by PID.

    python tests/playwright_phone.py [--keep-workdir]

Two fake producer terminals stand in for shells (the /browserland hello) and
echo each resize as `resized`. Checks print PASS/FAIL; the exit code is the
failures.

  - phone 390x844 (is_mobile, touch): innerWidth is 390 (not the 980px legacy
    default), a tiled terminal fits < 30 cols, #help-chip is on screen, the
    window chips in #taskbar-items keep > 40px, the clock chip is hidden, and
    the title text is readable.
  - desktop 1600x1000: the page served as-is and the page with the viewport
    meta + the whole #249 CSS block stripped out have IDENTICAL rects for every
    element with an id, identical term cols/rows and title-bar button rects.
    That proves the additions are a no-op for a desktop browser; it observes
    layout, not a flag.
"""
import argparse, asyncio, json, os, re, secrets, shutil, socket, subprocess
import sys, tempfile, threading, time, urllib.request
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
_ap = argparse.ArgumentParser()
_ap.add_argument("--keep-workdir", action="store_true")
_args = _ap.parse_args()

TOKEN = secrets.token_hex(16)
FAILS = []
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")


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


PORT = free_port()
BASE = "http://127.0.0.1:%d" % PORT
WORKDIR = Path(tempfile.mkdtemp(prefix="bl249-"))
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


class Bg:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


BG = Bg()


class Producer:
    async def start(self, wid, title):
        self.ws = await websockets.connect(
            "ws://127.0.0.1:%d/browserland?token=%s" % (PORT, TOKEN), max_size=None)
        await self.ws.send(json.dumps({"type": "hello", "window_id": wid, "pid": 7,
                                       "title": title, "cols": 80, "rows": 24}))
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

    async def close(self):
        await self.ws.close()


WIDS = (249901, 249902)
PRODS = [Producer() for _ in WIDS]
for p, w in zip(PRODS, WIDS):
    BG.run(p.start(w, "term-%d" % w))
KEYS = ["local:%d" % w for w in WIDS]

MEASURE = r"""
() => {
  const r = (el) => { const b = el.getBoundingClientRect();
    return [Math.round(b.left), Math.round(b.top), Math.round(b.right), Math.round(b.bottom)]; };
  const ids = {};
  for (const e of document.querySelectorAll('[id]')) ids[e.id] = r(e);
  const bar = document.querySelector('.term-window .title-bar');
  const cc = document.getElementById('clock-chip');
  const hc = document.getElementById('help-chip');
  const tt = bar && bar.querySelector('.title-text');
  return {
    innerWidth, ids,
    cols: [...windows.values()].map(w => [w.term.cols, w.term.rows]),
    bar: bar ? [...bar.children].map(c => [c.className, r(c)]) : [],
    barRect: bar ? r(bar) : null,
    // after flipping the first terminal's rec button to "recording", is it shown
    // inside the title bar? (it is the only way to stop a recording)
    recWhileRecording: (() => {
      const b = bar && bar.querySelector('.btn-rec');
      if (!b) return null;
      b.classList.add('recording');
      const x = r(b), shown = getComputedStyle(b).display !== 'none' && x[2] > x[0];
      b.classList.remove('recording');
      return {shown, rect: x};
    })(),
    clockShown: !!cc && getComputedStyle(cc).display !== 'none',
    help: hc ? r(hc) : null,
    items: r(document.getElementById('taskbar-items')),
    titleW: tt ? Math.round(tt.getBoundingClientRect().width) : 0,
  };
}
"""


def strip_249(html):
    """The page minus the viewport meta and the whole #249 CSS block."""
    meta = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    assert html.count(meta) == 1, "the viewport meta is not in the page exactly once"
    html = html.replace(meta, "")
    out, n = re.subn(r"/\* ---- #249 phone layout.*?/\* ---- end #249 phone layout \*/",
                     "", html, flags=re.S)
    assert n == 1, "the #249 CSS block markers were not found exactly once"
    return out


def load(browser, ctx_kw, rewrite=None):
    ctx = browser.new_context(**ctx_kw)
    if rewrite:
        def handler(route):
            if route.request.resource_type != "document" or "/?token=" not in route.request.url:
                return route.continue_()
            resp = route.fetch()
            return route.fulfill(response=resp, body=rewrite(resp.text()))
        ctx.route("**/*", handler)
    page = ctx.new_page()
    page.goto(BASE + "/?token=" + TOKEN)
    page.wait_for_function(
        "(ks) => typeof sessions !== 'undefined' && ks.every(k => sessions.has(k))",
        arg=KEYS, timeout=25000)
    page.evaluate("(ks) => { for (const k of ks) if (!windows.has(k)) openWindow(k, sessions.get(k));"
                  " if (!isTilingMode()) enterTilingMode(); }", KEYS)
    page.wait_for_timeout(3000)
    m = page.evaluate(MEASURE)
    ctx.close()
    return m


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        phone_kw = dict(viewport={"width": 390, "height": 844}, user_agent=UA,
                        has_touch=True, is_mobile=True, device_scale_factor=3)
        ph = load(browser, phone_kw)
        check(ph["innerWidth"] == 390, "phone: innerWidth is 390 (got %s)" % ph["innerWidth"])
        check(bool(ph["cols"]) and all(c[0] < 30 for c in ph["cols"]),
              "phone: a tiled terminal fits < 30 cols (got %s)" % ph["cols"])
        check(ph["help"] is not None and ph["help"][2] <= 390,
              "phone: #help-chip right edge <= 390 (got %s)" % (ph["help"],))
        check(ph["items"][2] - ph["items"][0] > 40,
              "phone: #taskbar-items keeps > 40px (got %s)" % (ph["items"],))
        check(not ph["clockShown"], "phone: the clock chip is hidden")
        check(ph["titleW"] >= 40, "phone: the title text is readable, >= 40px (got %s)" % ph["titleW"])
        keep = [(c, b) for c, b in ph["bar"] if "btn-close" in c or "btn-min" in c or "mcp-btn" in c]
        check(len(keep) == 3 and all(b[2] - b[0] >= 30 for c, b in keep),
              "phone: min/close/mcp stay and are >= 30px wide touch targets (got %s)" % keep)
        bx = ph["barRect"]
        check(all(b[0] >= bx[0] and b[2] <= bx[2] for c, b in keep),
              "phone: min/close/mcp sit inside the title bar %s" % (bx,))
        check(ph["recWhileRecording"]["shown"] and ph["recWhileRecording"]["rect"][2] <= bx[2],
              "phone: a recording terminal keeps its stop button (got %s)" % ph["recWhileRecording"])
        idle = [c for c, b in ph["bar"] if "btn-rec" in c and b[2] > b[0]]
        check(not idle, "phone: the idle record button is hidden (got %s)" % idle)
        check(all(r[2] <= 390 for k, r in ph["ids"].items()
                  if k in ("taskbar", "btn-launch", "taskbar-items", "host-status", "btn-fullscreen")),
              "phone: the taskbar pieces stay inside the 390px viewport")

        sm = load(browser, dict(phone_kw, viewport={"width": 320, "height": 568}))
        check(sm["innerWidth"] == 320 and sm["help"] is not None and sm["help"][2] <= 320,
              "phone 320x568: help chip on screen (got %s)" % (sm["help"],))
        check(sm["items"][2] - sm["items"][0] > 40,
              "phone 320x568: #taskbar-items keeps > 40px (got %s)" % (sm["items"],))
        sbx = sm["barRect"]
        check(all(b[0] >= sbx[0] and b[2] <= sbx[2] for c, b in sm["bar"] if "btn-close" in c or "btn-min" in c),
              "phone 320x568: min/close sit inside the title bar")

        ls = load(browser, dict(phone_kw, viewport={"width": 844, "height": 390}))
        check(ls["innerWidth"] == 844 and ls["help"] is not None and ls["help"][2] <= 844,
              "landscape 844x390: laid out at 844, help chip on screen")

        dk_kw = dict(viewport={"width": 1600, "height": 1000})
        a = load(browser, dk_kw)
        b = load(browser, dk_kw, rewrite=strip_249)
        diff = {k: (a["ids"].get(k), b["ids"].get(k)) for k in set(a["ids"]) | set(b["ids"])
                if a["ids"].get(k) != b["ids"].get(k)}
        check(len(a["ids"]) > 50 and not diff,
              "desktop 1600x1000: all %d id rects identical with/without the #249 additions %s"
              % (len(a["ids"]), list(diff.items())[:3]))
        check(bool(a["cols"]) and a["cols"] == b["cols"],
              "desktop: term cols/rows identical (%s)" % (a["cols"],))
        check(bool(a["bar"]) and a["bar"] == b["bar"], "desktop: title-bar child rects identical")
        check(a["clockShown"] == b["clockShown"], "desktop: clock chip state identical")
        browser.close()
finally:
    for p in PRODS:
        try:
            BG.run(p.close(), 5)
        except Exception:
            pass
    stop_broker()

print("%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
