"""#226 acceptance: "Open in new window" driven end to end in a real browser.

NOT collected by pytest (no test_ prefix) -- it needs a running broker and
Playwright's Chromium. Run it against a THROWAWAY broker (it launches a shell
on that broker's default profile and terminates it at the end):

    python -m webterm.broker --port 4446            # from a scratch cwd, with
                                                    # PYTHONPATH at the repo
    python tests/playwright_detach.py --base http://127.0.0.1:4446/         --token-file <that cwd>/webterm_token.json

The token is read from the file and never printed. Checks, in order: the
blocked-popup path is a no-op; the desktop releases (socket closed) BEFORE the
child builds its window; the child is the viewport-sized single float on a
forced-floating layout, holds the Web Lock, skips workspaces; the desktop is
detached + minimized + socket-less, chip marked, record persisted, and its
prefs blob untouched by the child; input/output flow only through the child;
restore paths keep the window minimized; a desktop reload rebuilds it detached
and unattached and the reattach poll leaves it alone; closing the child
returns it (snapshot replay lands); a second child refuses and its `bye` is
ignored; a desktop Close while detached still reopens when the child goes;
the child's own x returns it.
"""
import argparse, json, os, sys, time
from playwright.sync_api import sync_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--base", required=True, help="broker URL, e.g. http://127.0.0.1:4446/")
_ap.add_argument("--token-file", required=True, help="that broker's webterm_token.json")
_ap.add_argument("--headed", action="store_true")
_args = _ap.parse_args()
TOKEN = json.load(open(_args.token_file, encoding="utf-8"))["auth_token"]
BASE = _args.base if _args.base.endswith("/") else _args.base + "/"
FAILS = []

def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: FAILS.append(label)

def wait_for(page, js, timeout=15, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(js): return True
        except Exception:
            pass
        time.sleep(0.2)
    print("TIMEOUT waiting:", label or js)
    return False

WIN = """(key) => { const w = windows.get(key); if (!w) return null; return {
    detached: !!w.detached, minimized: !!w.minimized, wsOpen: !!w.wsOpen,
    hasWs: !!w.ws, disposed: !!w.disposed, tiled: !!w.tiled,
    rows: w.term.rows, cols: w.term.cols,
    buf: (() => { const b = w.term.buffer.active; let s=''; for (let i=0;i<b.length;i++) s += b.getLine(i).translateToString(true) + '\\n'; return s; })(),
}; }"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=not _args.headed)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    desk = ctx.new_page()
    desk.on("console", lambda m: print("  [desk console]", m.type, m.text) if m.type in ("error", "warning") else None)
    desk.goto(BASE + "?token=" + TOKEN)
    wait_for(desk, "typeof _booted !== 'undefined' && _booted && _stateReady", label="desktop boot")
    # launch a terminal through the UI's own path
    desk.evaluate("launchProfile(localHost(), hostDefaultProfile(localHost()))")
    ok = wait_for(desk, "Array.from(windows.values()).some(w => w.type !== 'app' && w.wsOpen)", 30, "terminal open + attached")
    check(ok, "terminal launched and attached on the desktop")
    key = desk.evaluate("Array.from(windows.keys()).find(k => windows.get(k).type !== 'app')")
    print("key:", key)
    before = desk.evaluate(WIN, key)
    check(before and before["wsOpen"] and not before["detached"], "desktop attached before detach")

    # popup blocked: window.open returns null -> nothing changes, a notice shows
    blocked = desk.evaluate("(k) => { const o = window.open; window.open = () => null; try { return detachWindow(k); } finally { window.open = o; } }", key)
    check(blocked is False, "blocked popup: detachWindow returns false")
    check(desk.evaluate("!!Array.from(document.querySelectorAll('#notice-host > *')).find(n => (n.textContent||'').includes('popup blocked'))"), "blocked popup: notice shown")
    check(desk.evaluate(WIN, key)["wsOpen"], "blocked popup: desktop still attached")
    # ---- detach via the public entry point (menu/keybinding call this) ----
    with ctx.expect_page() as popup_info:
        r = desk.evaluate("(k) => detachWindow(k)", key)
    child = popup_info.value
    child.on("console", lambda m: print("  [child console]", m.type, m.text) if m.type in ("error", "warning") else None)
    check(r is True, "detachWindow returned true (popup opened)")
    check("detach=" in child.url, "child url carries ?detach= : " + child.url.replace(TOKEN, "***"))
    wait_for(child, "typeof _booted !== 'undefined' && _booted", 15, "child boot")
    ok = wait_for(child, "windows.has(%s)" % json.dumps(key), 20, "child window created")
    early = desk.evaluate(WIN, key)
    check(ok and early and early["detached"] and not early["hasWs"], "handshake: desktop had released (socket closed) BEFORE the child built its window")
    ok = wait_for(child, "(() => { const w = windows.get(%s); return !!(w && w.wsOpen && w.dom.classList.contains('detached-main')); })()" % json.dumps(key), 20, "child attached + fitted")
    check(ok, "child page attached its one window, fitted (detached-main)")
    check(child.evaluate("document.body.classList.contains('detached-surface')"), "child body has detached-surface")
    check(child.evaluate("getComputedStyle(document.getElementById('taskbar')).display === 'none'"), "child taskbar hidden")
    check(child.evaluate("SURFACE === 'detached' && isDetachedSurface()"), "child SURFACE === detached")
    check(child.evaluate("prefs._layout.mode === 'floating' && (prefs._layout.columns || []).length === 0"), "child layout forced floating, no columns")
    check(child.evaluate("(k) => { const w = windows.get(k); return Math.abs(w.dom.offsetWidth - window.innerWidth) <= 2 && Math.abs(w.dom.offsetHeight - window.innerHeight) <= 2; }", key), "child window fills the viewport (no grid snap)")
    check(child.evaluate("(k) => { const w = windows.get(k); return !w.tiled; }", key), "child window is a float")
    lock_held = child.evaluate("(k) => navigator.locks.query().then(s => s.held.some(l => l.name === detachLockName(detachCanonical(k))))", key)
    check(lock_held, "child holds the Web Lock")
    check(child.evaluate("isModEnabled('workspaces') === false"), "workspaces skipped on the child surface")
    check(child.evaluate("isModEnabled('theme') === true"), "theme still enabled on the child surface")

    # ---- desktop yielded? ----
    ok = wait_for(desk, "(k) => { const w = windows.get(k); return !!(w && w.detached && w.minimized && !w.ws); }" if False else "(() => { const w = windows.get(%s); return !!(w && w.detached && w.minimized && !w.ws); })()" % json.dumps(key), 15, "desktop yield")
    after = desk.evaluate(WIN, key)
    check(ok and after["detached"] and after["minimized"] and not after["hasWs"], "desktop: detached + minimized + socket closed")
    check(desk.evaluate("(k) => !!document.querySelector('.taskbar-item[data-session-id=\"' + CSS.escape(k) + '\"].detached')", key), "desktop taskbar chip marked .detached")
    check(desk.evaluate("(k) => !!(prefs._detached && prefs._detached[k])", key), "desktop pref records the detached key")
    check(desk.evaluate("(k) => JSON.parse(localStorage.getItem('webterm:prefs:v1'))._detached[k] != null", key), "detached record persisted to localStorage")
    # the child never wrote the prefs blob: the blob's layout is still the desktop's
    check(child.evaluate("JSON.parse(localStorage.getItem('webterm:prefs:v1'))._layout.mode !== 'floating' || true"), "(info) blob layout mode read")
    desk_mode = desk.evaluate("prefs._layout.mode")
    blob_mode = desk.evaluate("JSON.parse(localStorage.getItem('webterm:prefs:v1'))._layout.mode")
    check(desk_mode == blob_mode, "localStorage layout still the desktop's (child did not clobber): %s == %s" % (desk_mode, blob_mode))

    # ---- input only reaches the PTY from the child; output only lands in the child ----
    marker = "DETACH_MARK_%d" % int(time.time())
    child.evaluate("(a) => { const w = windows.get(a[0]); w.ws.send(JSON.stringify({type:'input', data: 'echo ' + a[1] + '\\r'})); }", [key, marker])
    ok = wait_for(child, "(a) => { const w = windows.get(a[0]); const b = w.term.buffer.active; for (let i=0;i<b.length;i++) if (b.getLine(i).translateToString(true).includes(a[1])) return true; return false; }" if False else
                  "(() => { const w = windows.get(%s); const b = w.term.buffer.active; let n=0; for (let i=0;i<b.length;i++) if (b.getLine(i).translateToString(true).includes(%s)) n++; return n >= 2; })()" % (json.dumps(key), json.dumps(marker)), 20, "echo in child (cmd + output)")
    check(ok, "child terminal shows the echoed marker (input + output flow through the child)")
    time.sleep(1.0)
    dbuf = desk.evaluate(WIN, key)["buf"]
    check(marker not in dbuf, "desktop terminal did NOT receive the marker while detached")

    # ---- restore paths focus the child instead of un-minimizing ----
    desk.evaluate("(k) => restoreWindow(k)", key)
    time.sleep(0.5)
    st = desk.evaluate(WIN, key)
    check(st["detached"] and st["minimized"], "restoreWindow on a detached window keeps it minimized (focus-child branch)")
    desk.evaluate("(k) => onTaskbarClick(k)", key)
    time.sleep(0.5)
    st = desk.evaluate(WIN, key)
    check(st["detached"] and st["minimized"], "taskbar click on a detached window keeps it minimized")

    # ---- desktop reload while the child is open: adopt without attaching ----
    desk.reload()
    wait_for(desk, "typeof _booted !== 'undefined' && _booted && _stateReady", label="desktop reboot")
    ok = wait_for(desk, "(() => { const w = windows.get(%s); return !!(w && w.detached && w.minimized); })()" % json.dumps(key), 20, "reload adopt")
    st = desk.evaluate(WIN, key)
    check(ok and st and not st["hasWs"], "after desktop reload: window rebuilt detached, minimized, NOT attached")
    time.sleep(2.5)
    st = desk.evaluate(WIN, key)
    check(st and not st["hasWs"] and st["detached"], "…and the reattach poll left it alone")

    # ---- child closes (OS window close): lock releases, desktop returns ----
    child.close()
    ok = wait_for(desk, "(() => { const w = windows.get(%s); return !!(w && !w.detached && !w.minimized && w.wsOpen); })()" % json.dumps(key), 20, "desktop return")
    st = desk.evaluate(WIN, key)
    check(ok and st and st["wsOpen"] and not st["detached"], "child closed: desktop reattached and restored")
    check(desk.evaluate("(k) => !(prefs._detached && prefs._detached[k])", key), "detached record cleared")
    ok = wait_for(desk, "(() => { const w = windows.get(%s); const b = w.term.buffer.active; for (let i=0;i<b.length;i++) if (b.getLine(i).translateToString(true).includes(%s)) return true; return false; })()" % (json.dumps(key), json.dumps(marker)), 15, "snapshot replay on return")
    check(ok, "desktop terminal shows the marker after return (snapshot replay)")

    # ---- second child for the same key refuses (lock taken) ----
    with ctx.expect_page() as pi:
        desk.evaluate("(k) => detachWindow(k)", key)
    c1 = pi.value
    wait_for(c1, "(() => { const w = windows.get(%s); return !!(w && w.wsOpen); })()" % json.dumps(key), 20, "child #1")
    c2 = ctx.new_page()
    c2.goto(BASE + "?detach=" + key)
    wait_for(c2, "typeof _booted !== 'undefined' && _booted", 15, "child #2 boot")
    time.sleep(2.5)
    check(not c2.evaluate("(k) => windows.has(k)", key), "second child for the same key opened no window")
    check(c2.evaluate("!!Array.from(document.querySelectorAll('.notice-sticky')).find(n => (n._noticeText||'').includes('already open'))"), "second child says 'already open in another window'")
    # a refused child's bye must not return the real owner's terminal
    c2.evaluate("detachedChildReturn()")
    time.sleep(1.2)
    st = desk.evaluate(WIN, key)
    check(st and st["detached"] and not st["hasWs"], "refused child's `bye` ignored: desktop still detached")
    c1st = c1.evaluate(WIN, key)
    check(c1st and c1st["wsOpen"], "…and the owning child is still attached")
    try: c2.close()
    except Exception: pass
    # desktop menu Close while detached: the desktop forgets the window; when the child goes, the terminal comes back
    desk.evaluate("(k) => closeWindow(k)", key)
    time.sleep(0.5)
    check(not desk.evaluate("(k) => windows.has(k)", key), "desktop Close while detached forgot the window")
    check(desk.evaluate("(k) => !!(prefs._detached && prefs._detached[k])", key), "…but the detached record survives Close")
    c1.close()
    ok = wait_for(desk, "(() => { const w = windows.get(%s); return !!(w && !w.detached && w.wsOpen); })()" % json.dumps(key), 25, "reopen after Close + child gone")
    check(ok, "child gone after a desktop Close: terminal reopened and attached on the desktop")
    # fresh child for the explicit-Return check
    with ctx.expect_page() as pi:
        desk.evaluate("(k) => detachWindow(k)", key)
    c1 = pi.value
    wait_for(c1, "(() => { const w = windows.get(%s); return !!(w && w.wsOpen); })()" % json.dumps(key), 20, "child #3")
    wait_for(desk, "(() => { const w = windows.get(%s); return !!(w && w.detached); })()" % json.dumps(key), 15, "desktop yield #3")
    # explicit Return from the child's own × path
    c1.evaluate("detachedChildReturn()")
    ok = wait_for(desk, "(() => { const w = windows.get(%s); return !!(w && !w.detached && w.wsOpen); })()" % json.dumps(key), 20, "return via ×")
    check(ok, "explicit Return (child ×) brought the terminal back")
    time.sleep(0.5)
    check(c1.is_closed() or c1.evaluate("window.closed") or True, "(info) child page after Return")

    # ---- clean up: terminate the test shell ----
    desk.evaluate("(k) => terminateWindow(k)", key)
    time.sleep(2)
    browser.close()

print("\nFAILURES:", len(FAILS))
for f in FAILS: print(" -", f)
sys.exit(1 if FAILS else 0)
