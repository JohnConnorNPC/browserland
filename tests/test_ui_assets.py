"""Guards for the fragment-assembled desktop UI (issue #68).

The served page (``webterm/broker/ui.py``'s ``INDEX_HTML``) used to be one
~16.8k-line ``index.html``; #68 split it into ordered on-disk fragments that
``ui.py`` concatenates at import. These tests lock the acceptance criteria and
guard against regressions in the assembly:

* the assembled page still imports as a module-scope ``str`` and looks like the
  same document (DOCTYPE / ``</html>`` / no BOM / served-page sentinels);
* the monolith is gone and the UI is genuinely split into many small files
  (no multi-thousand-line script survives);
* the on-disk fragment set matches ``ui._ORDERED`` exactly -- no fragment is
  dropped from the package and no stray file is swept in.

Byte-identity vs the pre-split page is verified once, out of band, via a sha256
gate; it is deliberately NOT asserted here so ordinary UI edits stay free.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from webterm.broker import ui
from webterm.broker.ui import INDEX_HTML

BROKER_DIR = Path(ui.__file__).resolve().parent

# #194: the ctx-extender registry is BEHAVIOUR (ordering, per-extender
# isolation, identity-idempotence), and a source assertion cannot prove any of
# it. So those tests execute the shipped range in node, the way
# tests/test_host_registry_crypto.py executes the host-registry crypto -- and
# skip when node is absent, so the suite still runs on a box without it.
NODE = shutil.which("node")


def _declared_mod_css():
    """Every ``mods/<id>/<file>.css`` the in-repo manifests claim to ship, read
    STRICTLY (a bad manifest raises here, by design) and deduped in _MODS-then-
    `styles` order. This is the strict source of truth the drift + per-file
    guards compare against ui's best-effort ``_mod_css`` (#77/S4)."""
    out, seen = [], set()
    for mod_dir in dict.fromkeys(
            PurePosixPath(m).parent.as_posix() for m in ui._MODS):
        meta = json.loads((BROKER_DIR / mod_dir / "mod.json").read_text(encoding="utf-8"))
        for name in meta.get("styles", []):
            rel = f"{mod_dir}/{name}"
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


# --- #77/S4 fixture helpers: a synthetic fragment tree in tmp_path lets us drive
# ui.assemble() against a mod that actually ships .css without touching the real
# served page (which stays byte-identical because no in-repo mod declares css).
_SYNTH_ORDERED = [
    "00_head.html", "15_css_dialogs.css", "40_body.html",
    "86_js_mod_loader.js", "90_js_mod_boot.js", "99_tail.html",
]


def _write_synth_core(base):
    # Realistic head/body/script boundaries so position asserts mean what the
    # served page means: 15 is the last core css, 40 closes </style> + opens the
    # one <script>, 90 is loadMods(). The css/js splice anchors are the SAME
    # module constants assemble() uses.
    (base / "00_head.html").write_text("<!DOCTYPE html>\n<head><style>\n", encoding="utf-8")
    (base / "15_css_dialogs.css").write_text("/*DIALOGS*/\n", encoding="utf-8")
    (base / "40_body.html").write_text("</style></head>\n<body>\n<script>\n", encoding="utf-8")
    (base / "86_js_mod_loader.js").write_text("/*LOADER*/\n", encoding="utf-8")
    (base / "90_js_mod_boot.js").write_text("loadMods();\n", encoding="utf-8")
    (base / "99_tail.html").write_text("</script></body></html>\n", encoding="utf-8")


def _write_fixture_mod(base, mod_id, styles, files):
    md = base / "mods" / mod_id
    md.mkdir(parents=True, exist_ok=True)
    meta = {"id": mod_id, "ctxVersion": 1, "entry": f"{mod_id}.js", "styles": styles}
    (md / "mod.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    (md / f"{mod_id}.js").write_text(f"/*{mod_id.upper()}-JS*/\n", encoding="utf-8")
    for fn, content in files.items():
        (md / fn).write_text(content, encoding="utf-8")
    return f"mods/{mod_id}/{mod_id}.js"


# --------------------------------------------------------------------------- #
# assembled-page shape
# --------------------------------------------------------------------------- #

def test_index_html_is_module_scope_str():
    # Must stay a module-scope str so Sanic's _determine_error_format
    # introspection of the `return html(INDEX_HTML)` handler keeps working.
    assert isinstance(INDEX_HTML, str)
    assert INDEX_HTML, "assembled page is empty"


def test_index_html_document_boundaries():
    assert INDEX_HTML.startswith("<!DOCTYPE html>")
    assert INDEX_HTML.rstrip().endswith("</html>")
    assert INDEX_HTML.endswith("</html>\n"), "trailing newline must be preserved"


def test_index_html_has_no_bom():
    # A Windows editor / PowerShell write could prepend a UTF-8 BOM; the empty
    # join would then carry U+FEFF into the served bytes.
    assert "﻿" not in INDEX_HTML


def test_index_html_served_sentinels_present():
    for sentinel in (
        "<title>Browserland</title>",
        "term-window",
        "_hosts",
        "hostFetch",
        "hostUrl",
        "host-status",
        "set-profiles-list",     # #70 launch-profile editor markup
        "renderProfilesEditor",  # #70 editor logic
    ):
        assert sentinel in INDEX_HTML, f"missing served-page sentinel: {sentinel!r}"


def test_default_launch_host_wired_into_page():
    # #107: the START (+) button's default-host setting. No JS test runner exists
    # (pytest only), so lock the served-page symbols: the shared resolver, the
    # setting it reads, and the setting written by the Hosts UI Default button.
    for sentinel in (
        "function defaultLaunchHost",   # #107 shared resolver (56_js_hosts)
        "getSettings().defaultHost",    # #107 setting read (resolver) + written (hosts UI)
        "s.defaultHost = ''",           # #107 seeded/normalized in the settings model
    ):
        assert sentinel in INDEX_HTML, f"missing #107 sentinel: {sentinel!r}"


def test_host_added_after_load_note_wired_into_page():
    # #190 (client half): the CSP is per-document, so a host added mid-session
    # is unreachable from this document until reload -- the raw symptom is a
    # fetch() TypeError indistinguishable from network-down. 56_js_hosts.js
    # snapshots the host ids present at load and exposes a predicate + the
    # note string for a row-render layer to show; lock the served-page
    # symbols since no JS test runner exists (pytest only).
    for sentinel in (
        "const _hostIdsAtLoad",             # #190 one-time boot snapshot (56)
        "function hostAddedAfterLoad",      # #190 predicate (56)
        "const HOST_ADDED_NOTE",            # #190 the affordance's note text (56)
        "added — reload to connect",   # #190 the pinned wording itself
        "hostAddedAfterLoad(host.id)",      # #190 the row render consumes it (83)
        "host-added-note",                  # #190 the rendered span's class (83)
    ):
        assert sentinel in INDEX_HTML, f"missing #190 sentinel: {sentinel!r}"


def test_label_order_editor_wired_into_page():
    # #123: the configurable taskbar/title label order. No JS test runner exists
    # (pytest only), so lock the served-page symbols: the shell the editor mounts
    # into, the single label composer, the Control Panel editor, the self-healed
    # permutation field, and its normalizer (used by both the settings model and
    # the editor).
    for sentinel in (
        'id="set-label-order"',      # #123 editor mount point (40_body.html)
        "function composeLabelParts",  # #123 single label composer (64)
        "function renderLabelOrder",   # #123 Control Panel editor (81)
        "s.show.order",                # #123 permutation field, self-healed in 55
        "normalizeLabelOrder",         # #123 permutation normalizer (55, used by 81)
    ):
        assert sentinel in INDEX_HTML, f"missing #123 sentinel: {sentinel!r}"


def test_host_status_aggregate_wired_into_page():
    # #149: >1 broker collapses the taskbar chips into one aggregate badge and
    # moves per-host detail onto live broker rows in the start (+) menu. No JS
    # test runner exists (pytest only), so lock the served-page symbols: the
    # menu-state helpers and row builder (75), the aggregate renderer + its
    # badge CSS hook, the renderer's new keep-open field (77), and the
    # poll-driven guarded repaint of an open menu (76).
    for sentinel in (
        "function hostMenuState",      # #149 never-polled != down (75)
        "function hostStateSuffix",    # #149 shared state phrases (75)
        "function hostMenuItems",      # #149 the live broker row (75)
        "function renderAggregateChip",  # #149 the collapsed badge (75)
        ".host-chip.agg",              # #149 badge CSS hook (10)
        "ctx-swatch",                  # #149 row state dot (77 + 14)
        "keepOpen",                    # #149 hide-toggle keeps the menu open (77)
        "function repaintLaunchMenu",  # #149 owner+sig-gated menu refresh (76)
    ):
        assert sentinel in INDEX_HTML, f"missing #149 sentinel: {sentinel!r}"
    # The hide toggle must never close the menu via the generic click path:
    # renderMenu's dispatcher gates hideCtxMenu on the item's keepOpen flag.
    s77 = (BROKER_DIR / "77_js_context_menu.js").read_text(encoding="utf-8")
    assert "if (!it.keepOpen) hideCtxMenu();" in s77


def test_host_status_chip_visibility_wired_into_page():
    # #178: the broker-status chip gains a three-mode Control Panel option
    # (always / attention / never), and hidden brokers stop counting as needing
    # attention. No JS test runner exists (pytest only), so lock the served-page
    # symbols: the shared predicate and the visibility seam (75), the select
    # (40), the self-healed default (55), and the CSS hook (10).
    for sentinel in (
        "function hostNeedsAttention",          # predicate (75)
        "function applyHostStatusVisibility",   # visibility seam (75)
        'id="set-host-status-chip"',            # the 3-state select (40)
        "s.hostStatusChip = 'always'",          # self-heal default (55)
        "body.hide-broker-chip #host-status",   # CSS hook (10)
    ):
        assert sentinel in INDEX_HTML, f"missing #178 sentinel: {sentinel!r}"
    s75 = (BROKER_DIR / "75_js_taskbar_hosts.js").read_text(encoding="utf-8")
    body = s75.split("function hostNeedsAttention", 1)[1].split("}\n", 1)[0]
    # The predicate must read hostMenuState, NOT hostChipState: pollStateFor
    # seeds polling:false, which hostChipState reads as 'down' before a poll has
    # been TRIED, so a freshly-added host would flash the chip on for one tick.
    assert "hostMenuState(" in body
    assert "hostChipState(" not in body
    # A parked broker is not a fault — that is the whole of fix (2).
    assert "if (host.hidden) return false;" in body
    # The badge's count and color derive from the predicate / the live hosts,
    # never from the raw `states` array (which is per-host tooltip detail and
    # still lists hidden brokers).
    agg = s75.split("function renderAggregateChip", 1)[1].split(
        "function renderHostStatus", 1)[0]
    assert "hosts.filter(hostNeedsAttention).length" in agg
    assert "const live = hosts.filter(h => !h.hidden);" in agg
    assert "states.filter(" not in agg
    # Ordering is load-bearing: renderHostStatus's hottest call site is
    # unguarded, so the newest call goes LAST — a throw in it can at worst lose
    # a visibility update, never the rest of the tick.
    tail = s75.split("function renderHostStatus", 1)[1]
    assert tail.index("repaintLaunchMenu();") < tail.index(
        "applyHostStatusVisibility(hosts);")


def test_agent_and_cwd_frames_handled_in_browser():
    # #156: protocol.py advertises `agent` and `cwd` as broker->browser pushes,
    # but the ws.onmessage if-chain in 73_js_window_runtime used to drop both —
    # they fell off the end of the chain and the data only landed on the ~2 s
    # /sessions poll. No JS test runner exists (pytest only), so lock the served
    # -page symbols: both branches, both merges, and the shared seed helper.
    for sentinel in (
        "data.type === 'agent'",       # #156 branch (73)
        "data.type === 'cwd'",         # #156 branch (73)
        "sess.agent = String(data.data || '')",  # #156 merge into the map (73)
        "sess.cwd = String(data.data || '')",    # #156 merge into the map (73)
        "const liveSess = ()",         # #156 seed shared with the title branch (73)
    ):
        assert sentinel in INDEX_HTML, f"missing #156 sentinel: {sentinel!r}"
    # The paste wrap reads sess.agent, which now arrives straight off a JSON
    # frame — the lookup must be an own-property test so an inherited key
    # ('constructor', 'toString') can't read truthy and bracket a paste (67).
    assert re.search(r"Object\.prototype\.hasOwnProperty\.call\(\s*"
                     r"BRACKET_GAP_AGENTS,\s*sess\.agent\)", INDEX_HTML), \
        "needsConptyPasteWrap must use an own-property test on BRACKET_GAP_AGENTS"
    # ...and the map itself stays restricted to the live-verified agent (#138).
    assert "const BRACKET_GAP_AGENTS = { claude: true };" in INDEX_HTML, \
        "BRACKET_GAP_AGENTS must not be extended without a live-verified agent"


def test_sticky_notes_use_a_monospace_font():
    # Notes render in the shared monospace stack (Consolas/'Liberation Mono'),
    # so pasted code, ASCII, and aligned columns line up glyph-for-glyph — NOT
    # the old proportional Segoe UI. Pinned here so a CSS edit to
    # `.term-window.app-note .app-textarea` can't revert it silently. The 600
    # slice reaches the actual font-family line (the leading comment mentions
    # "monospace" too, so assert on the declaration, not just the word).
    note_rule = INDEX_HTML.split(".term-window.app-note .app-textarea")[1][:600]
    assert "font-family: Consolas, 'Liberation Mono', monospace;" in note_rule
    assert "Segoe UI" not in note_rule


def test_ws_switcher_preview_honors_live_filter():
    # #147: the switcher preview must apply the SAME liveness filter as the strip
    # (isLiveKey), so minimized/dormant/phantom windows aren't drawn as tiles.
    # isLiveKey is now a single shared helper both relayoutStrip and showWsPreview
    # use — pin that it's defined exactly once, and pin the ACTUAL predicates the
    # preview runs (not just the word, which a comment could satisfy): the per-row
    # live filter, the tolerant per-cell live filter, and the empty-state gate that
    # now keys off the live render set rather than the raw column count.
    assert INDEX_HTML.count("function isLiveKey") == 1
    preview = INDEX_HTML.split("function showWsPreview")[1][:7500]
    assert "rowKeys(row).filter(isLiveKey)" in preview
    assert "(Array.isArray(cell.keys) ? cell.keys : [])" in preview
    assert "!renderCols.length && !floatN" in preview


def _drag_resize_src():
    return (BROKER_DIR / "74_js_drag_resize.js").read_text(encoding="utf-8")


def test_window_gestures_take_pointer_capture():
    # #176: both gestures tracked on bare document mousemove/mouseup, so
    # content that swallows events (an iframe, whose events belong to ITS
    # document; a canvas that stopPropagation's) starved the listeners and the
    # gesture stalled mid-move with its mouseup never arriving. UI JS never runs
    # in CI, so pin the wiring by source: the capture is taken, it is only
    # BELIEVED when the UA confirms it, and both gestures ask for it.
    src = _drag_resize_src()
    assert "el.setPointerCapture(pd.id)" in src
    assert "if (!el.hasPointerCapture(pd.id)) return null;" in src
    assert src.count("captureGesturePointer(livePointerDown(), e.target, handle)") == 2
    # Tracking is pointer events in the document CAPTURE phase, filtered to the
    # captured pointer so a second pointer cannot corrupt the gesture.
    for name in ("pointermove", "pointerup", "pointercancel", "lostpointercapture"):
        assert f"document.addEventListener('{name}', p" in src
        assert f"document.removeEventListener('{name}', p" in src
    assert "if (ev.pointerId === cap.id)" in src
    # pointercancel (a touch/pen interruption sends no pointerup) and an
    # unexpected lostpointercapture end the gesture on the SAME path a lost
    # mouseup does, for both gestures.
    assert src.count("bindGestureTracking(cap, handle, onMove, onUp, onAbort)") == 2
    # A chorded release produces a mouseup and no pointerup (pointerdown/up fire
    # only on the 0<->1 buttons transition), so mouseup still ends the gesture --
    # but only the PRIMARY button's. This listener is deliberately the one that
    # sees everything, so without the filter a right-click landing mid-drag would
    # end it on the way out, which onDown already refuses to do on the way in.
    assert "const mUp = (ev) => { if (ev.button === 0) onUp(ev); };" in src
    assert "document.addEventListener('mouseup', mUp, true);" in src
    assert "document.removeEventListener('mouseup', mUp, true);" in src
    # Capture is released on every exit that is not an implicit release.
    assert "cap.el.releasePointerCapture(cap.id);" in src


def test_window_gestures_still_start_on_mousedown():
    # #176: the pointer id is sniffed from the pointerdown that precedes the
    # press, but the gesture START stays a mousedown -- a title-bar child's only
    # way to opt out of dragging the window is stopPropagation on its OWN
    # mousedown (core min/close/colour/MCP + every mod with a title-bar
    # control). pointerdown fires first and is a separate dispatch, so starting
    # there would ignore all of them and let press-and-hold on the close button
    # snap the window to the grid.
    src = _drag_resize_src()
    assert src.count("handle.addEventListener('mousedown', onDown);") == 2
    assert "handle.addEventListener('pointerdown'" not in src
    # Two pointerdown listeners, and NEITHER starts a gesture: the passive id
    # sniffer, which records mouse/pen only (a touch's compatibility mousedown
    # arrives after the touch has ENDED, so its id would be dead by the time we
    # tried to capture it), and the live gesture's last-resort recovery, which
    # only ends one.
    assert src.count("document.addEventListener('pointerdown'") == 2
    assert "const pDown = (ev) => { onCancel(ev); };" in src
    assert "document.removeEventListener('pointerdown', pDown, true);" in src
    assert "e.pointerType === 'mouse' || e.pointerType === 'pen'" in src
    assert "e.isPrimary" in src
    # The record is cleared by pointerup/pointercancel and by NOTHING else. A
    # per-read reset would contradict that: pointerdown/pointerup fire only on
    # the 0<->1 buttons transition, so with a second button already held a left
    # mousedown gets neither -- consuming the id on the first gesture would
    # leave the next one with no capture at all, silently back on the bug.
    assert "function livePointerDown() { return _lastPointerDown; }" in src
    assert src.count("() => { _lastPointerDown = null; }") == 2


def test_drag_pointer_events_none_stays_an_elementfrompoint_concern():
    # #176 asks for this by name: the dragged window's pointer-events:none is
    # there so elementFromPoint can find the window UNDERNEATH for swap/tab
    # mode. It is NOT what keeps the gesture alive over event-eating content --
    # reading it that way is how the resize path, which never had it, kept
    # stalling. Keep the line AND keep the warning attached to it.
    src = _drag_resize_src()
    assert src.count("win.dom.style.pointerEvents = 'none';") == 1
    preamble = src.split("win.dom.style.pointerEvents = 'none';")[0][-1400:]
    assert "elementFromPoint ONLY" in preamble
    assert "NOT what keeps the gesture alive" in preamble
    # ...and the warning must name what DOES keep it alive. Pointing at the
    # capture was wrong for the case the issue is about: a cross-site iframe is
    # hit-tested in another process before the capture is consulted.
    assert "content -- the shield is." in preamble


def test_gestures_raise_a_shield_that_every_exit_path_lowers():
    # #176 measured: over a genuine out-of-process iframe the capturing element
    # reported hasPointerCapture() true and our document still got 0 of 16
    # pointermoves, no pointerup and no pointercancel. Capture is a routing rule
    # inside ONE renderer, so the gesture also covers the viewport with a
    # transparent shield -- with it on top nothing underneath is hit-tested.
    src = _drag_resize_src()
    assert "_shieldEl.id = 'gesture-shield';" in src
    # Raised inside bindGestureTracking and lowered by the unbind it returns, so
    # it rides the SAME teardown that already covers pointerup, a plain mouseup,
    # pointercancel, a lost capture, blur and window disposal.
    assert "const lowerShield = raiseGestureShield(handle);" in src
    bind = src.split("function bindGestureTracking(")[1].split("\n        }")[0]
    assert bind.count("return () => {") == 2         # captured + fallback unbind
    # ONLY under a capture. Without one the teardown has nothing but a mouseup
    # that may never come, and a stuck shield makes the whole page unclickable;
    # worse, the compat mouseup would land on the SHIELD, so the click the UA
    # synthesises from a stationary press would go to the common ancestor
    # instead of the title bar and dblclick-to-rename would stop working.
    assert bind.count("lowerShield();") == 1
    fallback = bind.split("const lowerShield")[0]
    assert "raiseGestureShield" not in fallback
    # Backstop for a release nobody delivered: no button is down during a move
    # that belongs to a live gesture.
    assert "if (ev.buttons === 0) { onCancel(ev); return; }" in src
    # Refcounted: a gesture that lost its release is only swept when its own
    # handle is pressed again, so a second gesture can start under it and the
    # first to end must not strip the other's shield. Re-attach is keyed on
    # isConnected, not the refcount, so a node ripped out from under us
    # self-heals on the next gesture instead of never coming back.
    assert "_shieldRefs = Math.max(0, _shieldRefs - 1);" in src
    assert "if (!_shieldEl.isConnected) document.body.appendChild(_shieldEl);" in src
    # Above every other layer (the highest shipped is #auth-overlay at 250000).
    shield = INDEX_HTML.split("#gesture-shield {")[1].split("}")[0]
    assert "position: fixed;" in shield
    assert "inset: 0;" in shield
    assert int(shield.split("z-index:")[1].split(";")[0]) > 250000


def test_swap_probe_reads_through_the_shield():
    # With a shield on top, a plain elementFromPoint answers "the shield" for
    # every position, which would break swap (Shift) and tab (Alt) mode. Read
    # past it with elementsFromPoint -- READ-ONLY, where toggling the shield's
    # pointer-events off around the probe would write style twice per move and,
    # if anything in between threw, leave the shield inert.
    src = _drag_resize_src()
    assert "const hit = hitTestUnderShield(ev.clientX, ev.clientY);" in src
    probe = src.split("function hitTestUnderShield(")[1].split("\n        }")[0]
    assert "document.elementsFromPoint(x, y)" in probe
    assert "if (stack[i] !== _shieldEl) return stack[i];" in probe
    # The pointer-events toggle survives ONLY as the no-elementsFromPoint
    # fallback, and it restores in a finally.
    assert "if (document.elementsFromPoint) {" in probe
    assert "finally { _shieldEl.style.pointerEvents = prev; }" in probe
    # No bare elementFromPoint call is left on the gesture path.
    assert src.count("document.elementFromPoint(") == 2   # both inside the probe


def test_post_gesture_click_guard_is_scoped_to_the_capture_element():
    # The guard exists for ONE event: the compat click a captured pointerup
    # retargets onto the gesture's capture element. Unscoped it was a page-wide
    # outage -- `eat` sat on window in the capture phase and killed every trusted
    # click for a tick and every trusted DBLCLICK for 700ms, taking out the
    # browse pane's navigate/open rows, scratchpad tab renames, and the
    # app-window rename on a DIFFERENT window (the thing it was written for).
    src = _drag_resize_src()
    assert "if (!ev.isTrusted || !root.contains(ev.target)) return;" in src
    assert "function swallowClickAfterGesture(root)" in src
    # The drag passes its own handle -- its title bar. Not the whole page, and
    # not just the capture element: swallowing a click does not un-count it, so
    # the pair that reaches dblclick can be (retargeted click on the id badge,
    # then a click on the title text) and the dblclick lands on their common
    # ancestor. Everything that pairing can reach is inside the handle.
    assert src.count("swallowClickAfterGesture(handle)") == 1
    # ...and the resize does not guard at all: its capture element IS the
    # original mousedown target, a .rh grip with no click/dblclick handler, so
    # the retargeted click dies there and a guard would be pure collateral.
    assert src.count("swallowClickAfterGesture(") == 2   # the definition + 1 call
    resize = src.split("function wireResize(")[1]
    assert "swallowClickAfterGesture" not in resize.split("const onUp")[1]


def test_gesture_cleanup_covers_blur_cancel_and_disposal():
    # Before #176 the resize path's ONLY listener remover was its mouseup: a
    # blur (alt-tab with the button down) left document mousemove/mouseup bound
    # forever, so a later BUTTONLESS mousemove kept resizing the window. Both
    # gestures now share the abort path and both end on window teardown.
    src = _drag_resize_src()
    assert src.count("window.addEventListener('blur', onAbort);") == 2
    assert src.count("window.removeEventListener('blur', onAbort);") == 2
    # 2 win.cleanups hooks + the stale-gesture sweep at the top of each onDown.
    assert src.count("if (endGesture) endGesture();") == 4
    # A window disposed mid-resize has no geometry worth persisting and no
    # session left to resize.
    assert "if (!commit || win.disposed) return;" in src


def test_gesture_handles_declare_touch_action_none():
    # #176: the gestures are captured-pointer gestures, so a UA that claims a
    # pen (or touch) drag starting on a handle as a pan/zoom would
    # pointercancel it out from under us. Neither handle is a scroll surface.
    assert ".term-window:not(.tiled) .title-bar { touch-action: none; }" in INDEX_HTML
    rh = INDEX_HTML.split(".term-window .rh {")[1][:200]
    assert "touch-action: none" in rh
    # NOT on a tiled title bar. Tiled windows live in .strip-col columns inside
    # #strip, a real overflow-x:auto scroll container, so the rule would kill a
    # touch pan of the workspace -- and buys nothing there, because wireDrag
    # hands a tiled window to startTiledDrag before any capture is taken.
    bar = INDEX_HTML.split(".term-window .title-bar {")[1].split("}")[0]
    assert "touch-action" not in bar
    src = _drag_resize_src()
    tiled_handoff = src.index("startTiledDrag(win, e)")
    assert tiled_handoff < src.index("captureGesturePointer(livePointerDown()")


def test_index_html_never_puts_token_in_url():
    # Security invariant carried over from the monolith: the page must not push
    # the auth token into the address bar.
    assert "searchParams.set('token'" not in INDEX_HTML


# The token rides the query string (appendHostToken), so a URL built by one of
# these builders is a live credential. Fine in a fetch(); NOT fine in any sink
# the browser PERSISTS.
#: Since #144 the ONLY builder that can put a token in a URL is hostWsUrl --
#: HTTP goes through hostFetch, which sends Authorization: Bearer. hostHttpUrl,
#: appendHostToken and the recorder's recUrl are gone; they are still listed so
#: that reintroducing one under its old name trips this guard rather than
#: quietly restoring the leak.
_TOKEN_URL_BUILDERS = ("hostWsUrl(", "hostHttpUrl(", "appendHostToken(",
                       "recUrl(")

# Sinks that outlive the request. `.href` with a `download` attribute files the
# source URL in the browser's Downloads list; `window.open`/`location` put it in
# history and the address bar.
_PERSISTING_SINKS = (".href =", ".src =", ".action =", "window.open(",
                     "location.assign(", "location.replace(",
                     "location.href =")


def _ui_sources():
    """(name, text) for every fragment and mod script that ends up in the page."""
    for name in (*ui._ORDERED, *ui._MODS):
        if not name.endswith(".js"):
            continue
        yield name, (BROKER_DIR / name).read_text(encoding="utf-8")


def test_no_ui_source_puts_a_tokened_url_into_a_persisting_sink():
    """A token-bearing URL must never reach a sink the browser remembers.

    The bug this pins (recorder #142 follow-up): both recorder download buttons
    did ``a.href = recUrl('/recording?id=' + ...)`` with ``a.download`` set, so
    every download filed the live broker token into the browser's Downloads
    list -- on screen long after the session, re-triggerable via Retry, and
    synced across devices. The credential gates /launch and host-wide /file/*.

    ``test_index_html_never_puts_token_in_url`` above did NOT catch it: it only
    looks for ``searchParams.set('token'``. This scans every fragment and mod
    for a token-URL builder feeding a persisting sink ON THE SAME LINE, which
    is the shape the mistake actually takes. Downloads go through a Blob
    instead (``URL.createObjectURL``), whose blob: URL carries only the origin.

    Deliberately line-local: it cannot see ``const u = recUrl(p); a.href = u;``.
    That is the accepted limit of a source scan -- it catches the idiom people
    reach for, without banning ordinary anchors.
    """
    offenders = []
    for name, text in _ui_sources():
        for lineno, line in enumerate(text.splitlines(), 1):
            if not any(sink in line for sink in _PERSISTING_SINKS):
                continue
            if not any(b in line for b in _TOKEN_URL_BUILDERS):
                continue
            offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "token-bearing URL assigned to a persisted sink (use fetch + Blob + "
        "URL.createObjectURL instead):\n  " + "\n  ".join(offenders))


# --------------------------------------------------------------------------- #
# #143: third-party code must not execute unverified in the token's origin
# --------------------------------------------------------------------------- #

# Matches <script src="http..."> and <link ... href="http..."> in the assembled
# page. Only ABSOLUTE http(s) URLs -- same-origin relative assets are ours.
_EXTERNAL_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc=[\"'](https?://[^\"']+)[\"'][^>]*>", re.I)
_EXTERNAL_LINK_RE = re.compile(
    r"<link\b[^>]*\bhref=[\"'](https?://[^\"']+)[\"'][^>]*>", re.I)


def _external_asset_tags():
    """(url, full_tag) for every absolute-URL script/link in the served page."""
    for regex in (_EXTERNAL_SCRIPT_RE, _EXTERNAL_LINK_RE):
        for m in regex.finditer(INDEX_HTML):
            yield m.group(1), m.group(0)


def test_page_loads_no_third_party_asset_tags():
    """xterm is vendored (#143), so the page must fetch NO script or stylesheet
    from another origin.

    This is stricter than the SRI check it replaces, and deliberately so: SRI
    only helps while the hashes are right, and it left the app unusable offline.
    Same-origin means there is no CDN to compromise at all.

    The one remaining third party is CodeMirror from esm.sh -- loaded by dynamic
    import() from JS, never as a tag, so it is out of scope here and covered by
    the CSP origin test below.
    """
    external = [url for url, _tag in _external_asset_tags()]
    assert not external, (
        "third-party asset tags in the page -- vendor them under "
        "webterm/broker/vendor/ instead (see #143): " + ", ".join(external))


def test_vendored_assets_are_referenced_and_present_on_disk():
    """The flip side: the page must reference the vendored files, and every
    file it references must actually ship. A typo'd path is a blank terminal."""
    from webterm.broker import vendor

    referenced = set(re.findall(r"[\"'](/vendor/[^\"']+)[\"']", INDEX_HTML))
    # The CodeMirror graph (#146) is reached by a COMPUTED specifier --
    # `CM_BASE + 'entry-' + k + '.mjs'` -- so the page carries only the prefix,
    # never a whole filename. Its own coverage lives in test_vendor_assets.py.
    referenced.discard(vendor.CODEMIRROR_PREFIX)
    assert vendor.CODEMIRROR_PREFIX not in vendor._ASSETS

    assert referenced, "page references no vendored assets"
    for url in sorted(referenced):
        name = url[len(vendor.URL_PREFIX):]
        assert name in vendor._ASSETS, \
            f"{url} is referenced but not in the vendor allowlist"
        assert (BROKER_DIR / "vendor" / name).is_file(), \
            f"{url} is referenced but the file is missing from the wheel"
    # And nothing ships that the page never asks for (dead weight in the wheel).
    for name in vendor._ASSETS:
        assert vendor.URL_PREFIX + name in referenced, \
            f"vendored {name} is never referenced by the page"


def test_codemirror_loads_from_our_own_origin():
    """#146: the editor must import the vendored graph, not esm.sh.

    Pinned as a source assertion because the import specifier is computed at
    call time, so nothing else in the suite can see where it points."""
    from webterm.broker import vendor

    cm = (BROKER_DIR / "mods/editor/codemirror.js").read_text(encoding="utf-8")
    assert f"CM_BASE = '{vendor.CODEMIRROR_PREFIX}'" in cm
    assert "import(CM_BASE + 'entry-' + k + '.mjs')" in cm, \
        "the loader no longer imports the vendored entry modules"
    # CM_VER survives as the generator's INPUT, and the comment block above it
    # still explains esm.sh's resolution rules -- so prose may name the CDN.
    # Code may not: there must be no base URL left that import() could be
    # handed.
    assert "CM_CDN" not in cm, "CM_CDN is gone; the graph is served by us"
    for lineno, line in enumerate(cm.splitlines(), 1):
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        assert "esm.sh" not in line, \
            f"codemirror.js:{lineno} still reaches esm.sh from code: {line.strip()}"


def test_no_http_request_puts_the_token_in_the_url():
    """#144: the token rides `Authorization: Bearer`, never a query string.

    A URL credential leaks where a header cannot: any script on the page can
    read the full URL out of `performance.getEntriesByType('resource')`, a
    DevTools HAR export carries it into a bug report, and a reverse proxy
    (`tailscale serve` -- the topology SETUP.md recommends) logs it.

    The invariant is structural rather than stylistic: no function that builds
    a token-bearing HTTP URL exists any more, so one cannot be called by
    accident. A missed call site is a ReferenceError at load, which takes the
    whole bundle down loudly -- exactly what you want over a silent 401.
    """
    banned = ("function hostHttpUrl", "function appendHostToken")
    for name, text in _ui_sources():
        for b in banned:
            assert b not in text, (
                f"{name} reintroduces {b!r} -- HTTP must use hostFetch "
                "(Authorization: Bearer), see #144")
    # ...and nothing calls them either.
    for name, text in _ui_sources():
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue                      # prose may still name the old API
            for b in ("hostHttpUrl(", "appendHostToken("):
                assert b not in line, f"{name}:{lineno} calls removed {b}"


def test_websocket_is_the_only_remaining_token_in_url():
    """The documented exception, pinned so it stays deliberate.

    The browser WebSocket API cannot set request headers on the handshake, so
    /ws, /control and /browserland keep `?token=`. Closing that needs a
    connect-ticket scheme, not a refactor -- see wiki/Technical-Reference.md. This test
    exists so the exception can't quietly grow to cover HTTP again.
    """
    auth_src = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(
        encoding="utf-8")
    assert "function hostWsUrl" in auth_src
    assert "function hostFetch" in auth_src
    assert "function hostUrl" in auth_src
    # The token-appending code lives inside hostWsUrl and nowhere else.
    ws_start = auth_src.index("function hostWsUrl")
    ws_end = auth_src.index("\n        }", ws_start)
    assert "'token=' +" in auth_src[ws_start:ws_end], \
        "hostWsUrl must still carry the token (browsers can't send WS headers)"
    # hostFetch must NOT build a tokened URL.
    f_start = auth_src.index("function hostFetch")
    f_end = auth_src.index("\n        }", f_start)
    body = auth_src[f_start:f_end]
    assert "token=" not in body, "hostFetch must not put the token in the URL"
    assert "Authorization" in body and "Bearer" in body


def test_csp_hash_matches_the_inline_script_actually_served():
    """The one way the CSP bricks the app: a hash that doesn't match the bytes.

    ``script-src`` carries no ``'unsafe-inline'``, so if the ``'sha256-…'``
    source is off by so much as the newline after ``<script>``, the browser
    refuses to run the entire application and the user gets a blank page.

    This recomputes the digest independently of ui.inline_script_hash -- reading
    the served page and hashing the text child directly -- so an extraction bug
    in the helper shows up as a mismatch here rather than as a blank page.
    """
    import base64
    import hashlib

    from webterm.broker.app import _csp_header

    blocks = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)
    assert len(blocks) == 1, (
        f"expected exactly one inline <script>, found {len(blocks)} -- the CSP "
        "hash would authorize the wrong bytes")
    # Independently derived: no strip(), no normalization, exactly the element's
    # text child as the HTML parser sees it.
    expected = "sha256-" + base64.b64encode(
        hashlib.sha256(blocks[0].encode("utf-8")).digest()).decode("ascii")
    assert ui.inline_script_hash(INDEX_HTML) == expected
    assert f"'{expected}'" in _csp_header(expected)


def test_csp_authorizes_exactly_the_cdn_origins_the_page_uses():
    """Every origin the page loads script from must be in script-src, and
    nothing more -- an allowlist that drifts wider than the page is just a
    weaker policy, and one that drifts narrower breaks a feature.

    Since #146 vendored CodeMirror the correct answer is NO third-party origin
    at all, so this now pins an empty set on both sides. That is the strongest
    form of the same assertion, not a weaker one: script-src is 'self' plus our
    inline hash, and this origin holds prefs._hosts[].token for every
    configured host, so anything added back here is a fleet-wide trust
    decision."""
    from webterm.broker.app import _SCRIPT_ORIGINS

    used = set()
    for url, _tag in _external_asset_tags():
        used.add("/".join(url.split("/")[:3]))
    # CodeMirror is loaded by dynamic import(), not a tag, so it never appears
    # in _external_asset_tags -- pick up any absolute base URL from the source.
    cm = (BROKER_DIR / "mods/editor/codemirror.js").read_text(encoding="utf-8")
    for m in re.finditer(r"CM_(?:CDN|BASE)\s*=\s*'(https?://[^/']+)", cm):
        used.add(m.group(1))
    # 'self' is not a URL -- it covers the vendored /vendor/* scripts, which are
    # same-origin and therefore never appear as an absolute URL anywhere.
    allowed = set(_SCRIPT_ORIGINS) - {"'self'"}
    assert "'self'" in _SCRIPT_ORIGINS,         "vendored xterm is same-origin, so script-src must allow 'self'"
    assert used <= allowed, f"origins the page uses but CSP omits: {used - allowed}"
    assert allowed <= used, f"origins in CSP the page never uses: {allowed - used}"
    assert not allowed, f"#146 removed the last third-party origin; {allowed} is back"


def test_recorder_downloads_via_a_blob_not_a_tokened_anchor():
    """The positive half of the invariant above.

    An absence-assertion alone would also pass if someone deleted the download
    buttons outright, so pin that the replacement is actually there."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    assert "URL.createObjectURL(await r.blob())" in src
    assert "URL.revokeObjectURL(url)" in src, "blob URL must be revoked"
    # Both buttons go through the one helper.
    assert src.count("downloadRecording(") >= 3, \
        "expected the helper plus both call sites"
    # And a stale token must re-open the login prompt rather than silently
    # saving the 401 JSON body as a .blrec (the pre-fix behaviour). Since #161
    # the prompt names the broker the recording came FROM, not localHost().
    assert "promptFileHostAuth(host);" in src


def test_recorder_reaches_every_broker_not_just_the_local_one():
    """#161: the library lists recordings from every configured broker, and
    every per-recording op targets the broker that stores it.

    Source-slice asserts, so be honest about the limit: these prove the wiring
    is shaped right, NOT that a request lands on the right broker. That is what
    the two-broker live verification covers. What they do catch is the specific
    regression this change exists to remove -- a recorder call falling back to
    getHosts()[0]."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    # localHost() must not come back. It is the exact bug: every recorder route
    # hard-wired to getHosts()[0], so a recording on broker B was invisible from
    # a page attached to broker A. The one path that IS local-only (capture ->
    # upload) says so with a literal 'local', which is greppable and cannot
    # silently follow a reordered host list.
    assert "localHost()" not in src, \
        "recorder routes must name their broker, not default to getHosts()[0]"
    for route in ("/recording/begin", "/recording/chunk", "/recording/commit"):
        assert "recPost('local', '%s'" % route in src, \
            "capture uploads stay pinned to the local broker"
    # Every call carries a host id, and a host that no longer resolves is an
    # ERROR -- never a silent fall-through. hostFetch(null, path) targets the
    # SAME ORIGIN, so a stale id must not reach it: that would run a delete
    # against the local broker.
    assert "async function recApi(hostId, path, opts)" in src
    assert "function recPost(hostId, path, body)" in src
    assert "async function downloadRecording(hostId, recId, report)" in src
    assert re.search(r"const host = recHost\(hostId\);\s*\n\s*"
                     r"if \(!host\) return \{ ok: false, error: HOST_GONE \};",
                     src), "recApi must refuse a host it cannot resolve"
    # The fan-out itself, and the per-host isolation that keeps one dead broker
    # from emptying the list.
    assert "hosts.map(" in src and "'/recordings'" in src
    assert "recTry(" in src, "a transport failure is per-host, not fatal"
    assert "function buildHostError(" in src, \
        "a broker that did not answer gets a row, not a shorter list"
    # Ids are unique per BROKER only: anything keyed by one is composite.
    assert "function recKey(hostId, id)" in src
    assert "recKey(host.id, recId)" in src, "downloads keyed per (broker, id)"
    assert "recKey(hostId, r.series)" in src, "#151 chains keyed per broker"
    assert "'app:recplay:'" in src
    assert "encodeURIComponent(hostId)" in src, \
        "the player window id carries the broker, encoded"
    # A background repaint must never pop a login modal, and the player must be
    # retryable once its broker is signed into.
    assert "refresh({ prompt: false })" in src and \
        "refresh({ prompt: true })" in src
    assert "win._onHostAuth" in src
    # App windows keep hostId 'app' -- core reads win.hostId as the broker a
    # TERMINAL belongs to and drives masking/reattach off it.
    assert "hostId: 'app'" in src
    assert "hostId: recHostId" not in src


def test_update_check_names_the_broker_it_asks():
    """#182, and the same trap #161 closed for the recorder: a mod that reports
    a VERSION must never be able to report the wrong broker's version.

    hostFetch(host, path) builds its URL as ``(host && host.url) + path`` and
    only sends Authorization when host.token is set, so a null host is not an
    error -- it is a silent, unauthenticated request to the SERVING origin. In a
    version checker that failure is invisible by construction: the answer looks
    perfectly well-formed, it is just the local broker's answer wearing a peer's
    name. So the shape is pinned the same way the recorder's is.

    Source-slice asserts, so be honest about the limit: these prove the wiring
    is shaped right, NOT that a request lands on the right broker. What they do
    catch is the regression -- a call reaching hostFetch with anything other
    than a host resolved from an id at call time."""
    src = (BROKER_DIR / "mods/update/update.js").read_text(encoding="utf-8")
    # The exact bug, in its two spellings. localHost() resolves to getHosts()[0]
    # positionally, which is the local broker only by convention; hostFetch(null
    # ...) does not even do that much, it just hits whoever served the page.
    # Both are absent from the source ENTIRELY -- comments included, so the
    # grep stays a grep and cannot be defeated by a mention.
    assert "localHost()" not in src, \
        "the update mod must name its broker, not default to getHosts()[0]"
    assert "hostFetch(null" not in src, \
        "a null host is an unauthenticated call to the serving origin"
    # The positive half: hosts come from ids, at call time.
    assert "hostById(" in src, "hosts are resolved from an id, never captured"
    assert "function updHost(hostId)" in src
    assert "return hostById(hostId || LOCAL_HOST_ID);" in src
    assert "const LOCAL_HOST_ID = 'local';" in src, \
        "the local broker is a literal id, not a position in the host list"
    # EVERY hostFetch call site passes the resolved object, not an id, not null,
    # and not a captured record from an outer scope. Enumerated rather than
    # spot-checked so a second call site added later cannot slip past.
    firsts = re.findall(r"hostFetch\(\s*([^,\s)]+)", src)
    assert firsts, "the mod must actually call hostFetch"
    assert set(firsts) == {"host"}, \
        f"hostFetch call sites must pass the resolved host: {sorted(set(firsts))}"
    # ...and that object is obtained IMMEDIATELY before the request and fails
    # closed on a named state when the id no longer resolves. Falling through
    # to a null host is the one outcome that must be impossible.
    assert re.search(r"const host = updHost\(hid\);\s*\n\s*if \(!host\) \{", src), \
        "poll must resolve its host next to the request and refuse a null one"
    assert "st.error = 'no-such-host';" in src
    assert "'no-such-host':" in src, "the failure has words, not just a token"
    # Per-host state, so two brokers can hold different answers at once: one set
    # of module scalars could only ever describe one of them, and "this one is
    # current, that one is 3 behind" is the normal case for anyone running more
    # than one.
    assert "const hostChecks = new Map();" in src
    assert "function checkStateFor(hostId)" in src
    assert "checkStateFor(" in src
    # A broker we could not reach is NOT "could not reach GitHub". 'offline' is
    # the broker's own word for its egress failing; pinning it on a peer that is
    # merely asleep reports an outage that is not happening.
    assert "st.error = 'offline'" not in src, \
        "a transport failure to a broker must not be reported as GitHub's"
    assert "'unreachable':" in src and "'broker-error':" in src
    # It is the SERVED source that matters, not just the file on disk.
    assert "const LOCAL_HOST_ID = 'local';" in INDEX_HTML


def test_the_info_capability_is_carried_into_the_cached_catalog_record():
    """#185: the seam between GET /info and the update mod's capability probe.

    The broker publishes ``update: {check_enabled, apply_enabled}`` on /info and
    the mod's ``capabilityFrom`` reads ``rec.update`` as its most authoritative
    layer -- but NEITHER end is what carries the key between them.
    ``fetchModCatalog`` does, onto the record it caches, and that one line is
    the whole join. Both ends have tests of their own (the route's payload in
    the broker suite, the probe's behaviour in test_update_fleet.py), and both
    keep passing if the copy is dropped: the probe would silently lose layer 1
    and fall through to layers 2-4, where a headless peer that HAD published
    check_enabled becomes ambiguous ('unreachable-or-too-old') and a broker that
    already said "checking is off here" is spent a request to be told so again.
    A failure that quiet is exactly what a source-slice guard is for.

    Be honest about the limit: this proves the copy is written and guarded, not
    that a value survives a round trip."""
    panel = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    fetch = _frag_fn(panel, "async function fetchModCatalog(host)")
    # The initialiser. The field exists on EVERY cached record, the failure
    # shapes ('unreachable'/'unauthorized') included, so the probe reads a
    # deliberate null rather than `undefined` off a record nobody filled in.
    assert "update: null" in fetch, \
        "every cached catalog record must carry an `update` field"
    # The copy itself, and it stays GUARDED: `update` is untrusted input from a
    # peer's /info, and capabilityFrom's layer 1 dereferences it
    # (`upd.check_enabled === false`) the moment it is truthy.
    assert re.search(r"rec\.update = \(j\.update && typeof j\.update === "
                     r"'object'\)\s*\?\s*j\.update\s*:\s*null;", fetch), \
        "fetchModCatalog must copy a well-shaped j.update onto the record"
    # ...and the consumer reads that field, off the SHARED cache rather than
    # fetching /info a second time (two caches that can disagree about one peer
    # is how one pane says "asleep" while another says "fine").
    mod = (BROKER_DIR / "mods/update/update.js").read_text(encoding="utf-8")
    assert "const upd = rec.update;" in mod
    # Read through effectiveRecord (#182), which is modCatalogCache plus any
    # write this window performed since that record was last fetched. Still ONE
    # cache and still no second /info: the helper exists so the capability probe
    # and the on/off switch cannot disagree about the same broker.
    assert "function effectiveRecord(hostId)" in mod
    assert "modCatalogCache.get(hid)" in mod
    assert "capabilityFrom(effectiveRecord(host.id))" in mod
    # Both halves reach the SERVED page, not just the files on disk.
    assert "rec.update = (j.update && typeof j.update === 'object')" in \
        INDEX_HTML
    assert "const upd = rec.update;" in INDEX_HTML


def test_recorder_autorecord_setting_is_synced_and_default_off():
    """#151: the auto-record toggle rides the same synced settings primitive as
    the other mod toggles, and ships OFF so the mod stays inert until opted in.

    Source-slice asserts, so be honest about what they prove: that the wiring is
    present and defaults off, NOT that any gate holds at runtime. The behaviour
    (arming on create, the off switch, the roll) is what the live verification
    covers."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    m = re.search(r"ctx\.settings\.boolean\(\s*\n?\s*'recorder\.autoRecord',"
                  r"\s*(\w+),(.*?)\}\);", src, re.S)
    assert m, "recorder must own recorder.autoRecord via ctx.settings.boolean"
    assert m.group(1) == "false", \
        "auto-record must default OFF -- the key absent from the synced blob"
    assert "isBrowserGlobal: true" in m.group(2)
    # The setting is useless if it only arms terminals opened AFTER the flip
    # (onTerminalCreate has long since fired for the open ones), and dangerous
    # if flipping it off leaves a rolling recording running forever.
    assert "autoSetting.onChange(" in src
    assert "userStopped" in src, \
        "a manual stop must survive the next auto-record pass"
    # It reaches the served page (the mod scripts splice into one <script>).
    assert "'recorder.autoRecord'" in INDEX_HTML


def test_recorder_size_cap_rolls_instead_of_stopping():
    """#151: the cap opens a new segment rather than ending capture."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    assert "function rollRecording" in src and "function maybeRoll" in src
    # The cap is reached from the capture path via maybeRoll, never by ending
    # capture: the pre-#151 `if (rec.bytes > REC_CAP_BYTES) stopRecording(...)`
    # is what an always-on recording cannot survive.
    push = src[src.index("const pushOut = function"):
               src.index("rec.wrapWrite = function")]
    assert "maybeRoll(win, rec);" in push
    assert "stopRecording" not in push
    # ...and BOTH ceilings feed it: bytes bounds an output-heavy session, the
    # event count bounds an output-light one (input markers cost no bytes).
    roll = src[src.index("function maybeRoll"):src.index("function rollRecording")]
    assert "REC_CAP_BYTES" in roll and "REC_CAP_EVENTS" in roll
    # The roll is deferred by a task: nothing is lost (the old segment is still
    # patched until the timer fires) and the un-patch -> re-patch never runs
    # re-entrantly from inside term.write.
    assert "rec.rollTimer = setTimeout(" in roll
    # A failed replacement must be visible -- the previous segment is already
    # torn down, so a silent failure is exactly the symptom #151 removes.
    assert "showNotice(" in src[src.index("function rollRecording"):
                                src.index("function rollRecording") + 1200]
    # The chain rides the meta the server whitelists (#151), derived from `seg`
    # rather than baked into the stored title.
    assert "series: rec.series, seg: rec.seg," in src
    assert "'part ' + r.seg + '/' + parts" in src
    assert "' (part ' + meta0.seg + ')'" in src


def test_recorder_files_the_size_it_was_actually_played_at():
    """#151 arms capture from onTerminalCreate, where neither the grid nor the
    font is settled (termfont restyles from its own hook; core fits a round trip
    later), and meta.cols/rows is what a player window sizes itself to. So both
    are re-read at the first captured byte as well as at start -- guarded on an
    empty event list so a manual start never re-reads over a seed snapshot that
    was serialized at the size it was taken.

    The preferred source is win.lastSentDims, the grid core measured and handed
    to the agent, i.e. the size the PTY laid those bytes out for."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    push = src[src.index("const pushOut = function"):
               src.index("rec.wrapWrite = function")]
    assert "if (!rec.events.length) adoptGeom();" in push
    geom = src[src.index("const adoptGeom = function"):
               src.index("const pushOut = function")]
    # The grid core MEASURED and sent to the agent, not xterm's own grid, which
    # lags it by a round trip -- reading the terminal files a recording as 80x24
    # whose bytes were laid out for the real grid.
    assert "const dims = win.lastSentDims;" in geom
    for field in ("rec.cols = (dims && dims.cols) || t.cols;",
                  "rec.rows = (dims && dims.rows) || t.rows;",
                  "rec.fontFamily", "rec.fontSize"):
        assert field in geom, f"adoptGeom must refresh {field!r}"


def test_recorder_unload_guard_covers_saves_not_auto_recordings():
    """#151: the beforeunload prompt tracks what a reload would really destroy.

    An unconditional guard means auto-record prompts on EVERY reload; the
    pre-#151 guard dropped the instant capture stopped, so a reload during
    "saving..." lost the upload with no warning at all."""
    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    guard = src[src.index("function syncUnloadGuard"):]
    guard = guard[:guard.index("function newSeriesId")]
    assert "pendingSaves > 0" in guard
    assert "if (!r.auto)" in guard
    assert "active.size === 1" not in guard, \
        "the pre-#151 capture-only condition must be gone"
    assert "pendingSaves++" in src and "pendingSaves--" in src


def test_recorder_queues_uploads_under_the_broker_session_cap():
    """The broker 429s `too_many_sessions` past MAX_RECORDING_SESSIONS and the
    rejected segment is lost outright. #151 makes many-at-once ordinary -- the
    off switch stops every auto recording in one go, and a rolling terminal hands
    over a segment while the next fills -- so saves queue instead of racing."""
    from webterm.broker.app import MAX_RECORDING_SESSIONS

    src = (BROKER_DIR / "mods/recorder/recorder.js").read_text(
        encoding="utf-8")
    m = re.search(r"const SAVE_SLOTS = (\d+);", src)
    assert m, "the save queue must declare its concurrency"
    assert 0 < int(m.group(1)) < MAX_RECORDING_SESSIONS, (
        f"SAVE_SLOTS={m.group(1)} must stay under the broker's "
        f"MAX_RECORDING_SESSIONS={MAX_RECORDING_SESSIONS} so a manual save is "
        f"never starved by rolling segments")
    # Every stop goes through the queue, not straight at saveRecording.
    stop = src[src.index("function stopRecording"):
               src.index("// Uploads are QUEUED")]
    assert "enqueueSave(rec).then(" in stop
    assert "saveRecording(" not in stop
    # A rejection must not strand the counter: saveRecording serializes the whole
    # segment outside its own HTTP try/catch, so an allocation failure at rolling
    # sizes rejects -- and an armed reload guard would never disarm.
    pump = src[src.index("function pumpSaves"):src.index("async function saveRecording")]
    assert ".catch(function (e) {" in pump
    assert pump.index(".catch(") < pump.index(".then("), \
        "catch must precede then so the handler always runs"


# --------------------------------------------------------------------------- #
# the split actually happened
# --------------------------------------------------------------------------- #

def test_monolith_is_gone():
    assert not (BROKER_DIR / "index.html").exists(), \
        "the old monolithic index.html must be deleted"


def test_fragment_counts():
    js = list(BROKER_DIR.glob("*.js"))
    css = list(BROKER_DIR.glob("*.css"))
    assert len(js) >= 15, f"expected the JS split into many files, got {len(js)}"
    assert len(css) >= 2, f"expected the CSS split into >=2 files, got {len(css)}"


def test_no_multi_thousand_line_fragment():
    # The whole point of #68: no fragment is a giant script again. Mod scripts
    # (#71) and mod stylesheets (#77) ride the same cap.
    cap = 2500
    for name in (*ui._ORDERED, *ui._MODS, *_declared_mod_css()):
        lines = (BROKER_DIR / name).read_text(encoding="utf-8").count("\n")
        assert lines <= cap, f"{name} has {lines} lines (> {cap}); split it further"


def test_every_fragment_ends_in_newline_and_has_no_bom():
    # The empty-string join (#68) relies on each piece ending in its own \n; a
    # missing trailing newline fuses two statements/rules across a seam, and a
    # UTF-8 BOM mid-stream injects U+FEFF into the served bytes. Covers the mod
    # scripts (#71) AND mod stylesheets (#77), since both splice into the same
    # one <script> / one <style>.
    for name in (*ui._ORDERED, *ui._MODS, *_declared_mod_css()):
        raw = (BROKER_DIR / name).read_text(encoding="utf-8")
        assert raw.endswith("\n"), f"{name} must end in a newline"
        assert "﻿" not in raw, f"{name} carries a UTF-8 BOM"


def test_no_fragment_carries_a_raw_nul_byte():
    # #181 shipped `content: "\00a0"` into 15_css_dialogs.css with the escape
    # COLLAPSED into a literal 0x00. Two costs, and the second is the reason
    # this test exists: the rule can never render its intended NBSP (U+0000
    # parses as U+FFFD), and ripgrep classifies the whole file as BINARY, so
    # every rg-backed search, review and drift guard silently skips it. Python
    # read_text() passes NUL through happily, which is exactly why no existing
    # hygiene test caught it -- assert on the BYTES.
    for name in (*ui._ORDERED, *ui._MODS, *_declared_mod_css()):
        raw = (BROKER_DIR / name).read_bytes()
        assert b"\x00" not in raw, (
            f"{name} carries a raw NUL byte -- rg will treat it as binary and "
            f"silently miss every match in it")


# --------------------------------------------------------------------------- #
# assembly integrity
# --------------------------------------------------------------------------- #

def test_ordered_list_matches_disk_exactly():
    # Every fragment ui.py expects exists, and nothing else (no stray .bak /
    # Zone.Identifier / forgotten file) lives alongside them. Mismatch here is
    # exactly the failure mode the explicit _ORDERED list exists to prevent.
    # p.is_file() hardening (#71): the mods/ subdir is a directory, not a stray
    # fragment, so it must never count as "extra".
    ordered = set(ui._ORDERED)
    on_disk = {p.name for p in BROKER_DIR.iterdir()
               if p.is_file() and p.suffix in (".html", ".css", ".js")}
    missing = ordered - on_disk
    extra = on_disk - ordered
    assert not missing, f"fragments in _ORDERED but missing on disk: {sorted(missing)}"
    assert not extra, f"fragment-typed files on disk not in _ORDERED: {sorted(extra)}"


def _segment_join():
    """The served page as it exists ON DISK -- the five-segment fragment join,
    build stamp NOT yet substituted. #71 splices the mod scripts (ui._MODS) into
    the one <script> BETWEEN the loader and the boot fragment; #77 additionally
    splices each mod's manifest .css into the head <style> zone, AFTER
    ui._MOD_CSS_AFTER and before 40_body.html's </style>. So it is a 5-segment
    join, not a flat join of _ORDERED. Built the same way ui.assemble does
    (mod-css comes from the same best-effort ui._mod_css)."""
    css_cut = ui._ORDERED.index(ui._MOD_CSS_AFTER) + 1
    js_cut = ui._ORDERED.index(ui._MOD_SPLICE_BEFORE)

    def _j(names):
        return "".join((BROKER_DIR / n).read_text(encoding="utf-8") for n in names)

    return (
        _j(ui._ORDERED[:css_cut])
        + _j(ui._mod_css(ui._MODS, BROKER_DIR))
        + _j(ui._ORDERED[css_cut:js_cut])
        + _j(ui._MODS)
        + _j(ui._ORDERED[js_cut:])
    )


def test_assembled_equals_segment_join():
    # THE assembly invariant, in its post-#22 form: the served page is the
    # fragment join with the ONE build-stamp <meta> element rewritten, and
    # nothing else. Byte-identity with the raw join no longer holds (it cannot
    # -- the stamp is per-commit), so it is stated modulo exactly one
    # substitution: a named element, present once on disk, whose content becomes
    # webterm.build_version(). Everything the old assertion caught -- a dropped
    # fragment, a reordered splice, a stray edit in assemble() -- still fails
    # here; what it no longer forbids is that single named swap. Note it is the
    # ELEMENT that is swapped, not the bare token, so a `__WEBTERM_BUILD__`
    # appearing anywhere else must survive into the page verbatim.
    import webterm

    rebuilt = _segment_join()
    assert rebuilt.count(ui._BUILD_PLACEHOLDER) == 1, (
        "the build placeholder must appear exactly once on disk (in "
        "00_head.html), inside the stamp <meta>")
    assert rebuilt.replace(
        ui._BUILD_META.format(ui._BUILD_PLACEHOLDER),
        ui._BUILD_META.format(webterm.build_version())) == INDEX_HTML


def test_build_stamp_is_a_meta_tag_in_the_head():
    """#22: the served page names the build the broker PROCESS is running, so
    `curl -s <host>/ | grep webterm-build` vs `git rev-parse --short HEAD` says
    whether a deploy has actually restarted onto the pulled code.

    Pinned: the exact served line (that is the greppable contract), that the
    value is webterm.build_version(), and that there is exactly ONE such element
    -- counted as elements, not as substrings, so a future reader in JS
    (`document.querySelector('meta[name="webterm-build"]')`) is not a
    regression."""
    import webterm

    line = f'<meta name="webterm-build" content="{webterm.build_version()}">'
    assert line in INDEX_HTML
    assert INDEX_HTML.count('<meta name="webterm-build"') == 1
    # The comment beside the tag must not repeat the name: the head fragment is
    # what the documented one-line grep reads, and every extra mention there is
    # another line of noise in its output.
    head = (BROKER_DIR / "00_head.html").read_text(encoding="utf-8")
    assert head.count("webterm-build") == 1
    # In <head>, and no placeholder survived into the served bytes.
    assert INDEX_HTML.index("webterm-build") < INDEX_HTML.index("</head>")
    assert ui._BUILD_PLACEHOLDER not in INDEX_HTML


def test_build_stamp_does_not_change_the_csp_hash():
    """The stamp must NOT be inside the inline script.

    ``script-src`` authorizes that element by sha256 of its exact bytes, so a
    per-commit string in there would mint a new CSP hash on every commit --
    churn in app.py's policy for a value the head already carries. Assemble the
    real page twice with two different stamps: same hash, both times."""
    a = ui.assemble(version="0.0.0+aaaaaaa")
    b = ui.assemble(version="9.9.9+bbbbbbb")
    assert a != b, "the stamp must actually reach the page"
    assert ui.inline_script_hash(a) == ui.inline_script_hash(b) == \
        ui.inline_script_hash(INDEX_HTML)
    # ...and say it directly: the hashed bytes contain neither stamp.
    block = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)[0]
    assert "webterm-build" not in block and ui._BUILD_PLACEHOLDER not in block


def test_csp_hash_covers_the_script_that_starts_at_the_real_open_tag():
    """The failure the extraction regex can hide, now that the head has grown a
    comment: ``inline_script_hash`` anchors on the FIRST literal ``<script>``,
    which today is the real opening tag in 40_body.html (the vendor tags all
    carry ``src=``, and the ~15 other literal occurrences are in JS comments
    AFTER it). Write that token into any head fragment and the regex would
    start there and hash the wrong bytes -- a policy the browser rejects, i.e. a
    blank page, with the stamp-independence test above still passing.

    So pin the anchor rather than trusting it: the hashed block must begin with
    exactly what follows the opening tag inside 40_body.html."""
    body = (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    assert body.count("<script>") == 1, "40_body.html opens the one inline script"
    after_open = body.split("<script>", 1)[1]
    block = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)[0]
    assert block.startswith(after_open), (
        "the CSP hash is being computed from a <script> token that is not the "
        "real opening tag -- something before it in the page now contains the "
        "literal token")


def test_build_stamp_is_escaped_into_the_attribute():
    # The value is a hex short hash in practice, but it lands in a quoted
    # attribute -- a value that could close the attribute must not be able to.
    page = ui.assemble(version='x" onload="alert(1)')
    assert 'onload="alert(1)"' not in page
    assert "&quot; onload=&quot;alert(1)" in page


def test_mod_css_declared_matches_disk():
    # Drift guard for mod stylesheets (#77), the .css analogue of the _MODS .js
    # guard: every .css under mods/ is declared in some manifest's `styles`, and
    # every declared .css exists -- no orphan stylesheet silently absent from the
    # page, no dangling reference. (Both sides are empty until a mod ships css.)
    declared = set(_declared_mod_css())
    on_disk = {p.relative_to(BROKER_DIR).as_posix()
               for p in (BROKER_DIR / "mods").rglob("*.css")}
    assert declared == on_disk, (
        f"mods/ *.css drift: declared={sorted(declared)} on_disk={sorted(on_disk)}")


def test_mod_css_routed_into_head_style_zone(tmp_path):
    # A mod that ships a .css has it served INSIDE the still-open head <style>:
    # after the last core css fragment and before 40_body.html's </style>. Drive
    # the REAL ui.assemble against a synthetic fragment tree so the assertion
    # exercises production routing, not a parallel harness.
    _write_synth_core(tmp_path)
    js = _write_fixture_mod(tmp_path, "probe", ["probe.css"],
                            {"probe.css": "/*PROBE-CSS*/\n"})
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert page.count("/*PROBE-CSS*/") == 1
    assert page.index("/*DIALOGS*/") < page.index("/*PROBE-CSS*/") < page.index("</style>")
    # ...and the mod .js still splices between the loader and loadMods() (#71).
    assert page.index("/*LOADER*/") < page.index("/*PROBE-JS*/") < page.index("loadMods();")


def test_malformed_mod_css_skipped_best_effort(tmp_path):
    # A malformed mod css (here: no trailing newline) is skipped + logged, never
    # crashes assembly -- the broker still boots and the rest of the page (incl.
    # the mod's own .js) is unaffected. INDEX_HTML stays a module-scope str.
    _write_synth_core(tmp_path)
    js = _write_fixture_mod(tmp_path, "probe", ["probe.css"],
                            {"probe.css": "/*NO-NEWLINE*/"})  # missing trailing \n
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert "/*NO-NEWLINE*/" not in page          # the bad css is dropped
    assert "</style>" in page and "loadMods();" in page   # page still assembled
    assert "/*PROBE-JS*/" in page                # the mod's js is unaffected


def test_mod_css_rejects_unsafe_paths_and_dedupes(tmp_path):
    # Packaging/security edges: a `styles` entry that escapes the mod dir
    # ('../abs.css', '/abs.css'), nests ('nested/x.css'), or isn't css ('probe.js')
    # is rejected; a duplicate is emitted once. An out-of-dir abs.css that DOES
    # exist proves the '../' reference can't reach it.
    _write_synth_core(tmp_path)
    (tmp_path / "abs.css").write_text("/*ABS-ESCAPE*/\n", encoding="utf-8")
    styles = ["../abs.css", "/abs.css", "nested/x.css", "probe.js",
              "probe.css", "probe.css"]
    js = _write_fixture_mod(tmp_path, "probe", styles,
                            {"probe.css": "/*GOOD-CSS*/\n"})
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=[js], base=tmp_path)
    assert page.count("/*GOOD-CSS*/") == 1       # the one valid css, deduped to once
    assert "/*ABS-ESCAPE*/" not in page          # '../' / '/' never resolved out of dir
    assert "</style>" in page                    # assembly completed despite the junk
    # _mod_css returns exactly the one safe, repo-relative path.
    assert ui._mod_css([js], tmp_path) == ["mods/probe/probe.css"]


def test_mod_css_absent_styles_is_empty_and_noop(tmp_path):
    # A manifest with no `styles` (the state of every in-repo mod today) yields no
    # css segment, so the served page is byte-identical to the #71 join -- the
    # "no UI behavior change for existing features" guarantee.
    _write_synth_core(tmp_path)
    md = tmp_path / "mods" / "bare"
    md.mkdir(parents=True)
    (md / "mod.json").write_text(json.dumps({"id": "bare", "entry": "bare.js"}) + "\n",
                                 encoding="utf-8")
    (md / "bare.js").write_text("/*BARE-JS*/\n", encoding="utf-8")
    assert ui._mod_css(["mods/bare/bare.js"], tmp_path) == []
    page = ui.assemble(ordered=_SYNTH_ORDERED, mods=["mods/bare/bare.js"], base=tmp_path)
    # css zone is empty: </style> immediately follows the last core css.
    assert "/*DIALOGS*/\n</style>" in page


# --------------------------------------------------------------------------- #
# mod system (#71)
# --------------------------------------------------------------------------- #

def test_mod_loader_fragments_present_and_ordered():
    # The loader defines registerMod; the boot fragment runs loadMods() last.
    for frag in ("86_js_mod_loader.js", "90_js_mod_boot.js"):
        assert frag in ui._ORDERED, f"{frag} must be wired into _ORDERED"
    # loadMods() must be ordered after the loader so it's defined; the splice
    # point guarantees the mod scripts (registerMod) run before it.
    assert ui._ORDERED.index("86_js_mod_loader.js") \
        < ui._ORDERED.index("90_js_mod_boot.js")
    assert ui._MOD_SPLICE_BEFORE == "90_js_mod_boot.js"


# --------------------------------------------------------------------------- #
# the ctx-extender registry (#194) -- and the relocation that made room for it
# --------------------------------------------------------------------------- #

def test_ctx_extension_fragments_are_registered_and_ordered():
    # #194: new ctx surface may NOT land in 86_js_mod_loader.js -- it sits at the
    # #68 per-fragment cap, and the rule for that cap is split, never trim (86a
    # /#168 and 86b/#163 are the precedent). So the loader keeps ctx v1 + the
    # extender registry, 86c carries the families added after it, and 86d holds
    # the help-card family moved out to get back under the cap.
    for frag in ("86c_js_mod_ctx_ext.js", "86d_js_mod_help_cards.js"):
        assert frag in ui._ORDERED, f"{frag} must be wired into _ORDERED"
        assert (BROKER_DIR / frag).is_file(), f"{frag} missing on disk"
    # ...and both actually reach the served page (86c carries no code yet, so
    # its banner is what proves the splice happened).
    assert "// ---- ctx extensions (#194) ---" in INDEX_HTML
    assert "function _modRegisterHelpCards(" in INDEX_HTML
    at = ui._ORDERED.index
    # One <script>, in this order: the loader, then its companions, then the mod
    # scripts. Extenders run in registration order, which IS this order, so the
    # position of 86c is part of the contract rather than cosmetic -- and every
    # companion must precede the splice point, since a mod's init reads the
    # finished ctx.
    assert at("86_js_mod_loader.js") < at("86a_js_mod_settings_text.js") \
        < at("86b_js_mod_packages.js") < at("86c_js_mod_ctx_ext.js") \
        < at("86d_js_mod_help_cards.js") < at(ui._MOD_SPLICE_BEFORE)
    # The whole point: the loader is back under the cap and so is every
    # companion. (test_no_multi_thousand_line_fragment says this for every
    # fragment; said here too, because THIS is the constraint the split exists
    # to satisfy and the one a future edit will bump into first.)
    for frag in ("86_js_mod_loader.js", "86a_js_mod_settings_text.js",
                 "86b_js_mod_packages.js", "86c_js_mod_ctx_ext.js",
                 "86d_js_mod_help_cards.js"):
        lines = (BROKER_DIR / frag).read_text(encoding="utf-8").count("\n")
        assert lines <= ui._MAX_LINES, \
            f"{frag} has {lines} lines (> {ui._MAX_LINES}); split it further"


def test_ctx_extender_registry_is_the_seam_in_the_loader():
    # The registry is an ARRAY plus one apply pass, not a single shared
    # extension function: with one function, the moment a second extension
    # fragment declared its own the later declaration would win and the earlier
    # fragment's ctx members would silently vanish.
    loader = _loader_src()
    ctx_ext = _ctx_ext_src()
    # The registry itself moved to 86c (where every ctx extension lives); the
    # loader keeps only the CALL, so makeCtx stays the one place a ctx is built.
    for sym in ("const _ctxExtenders = [];",
                "function _registerCtxExtender(fn) {",
                "function _applyCtxExtenders(ctx, rec) {"):
        assert sym in ctx_ext, f"missing #194 registry symbol: {sym!r}"
        assert sym not in loader, (
            f"{sym!r} is back in the loader -- it belongs in 86c, and the "
            f"loader has no room for it")
    # makeCtx builds the v1 object, applies the registry to THAT object, and
    # returns it -- the apply must precede the return, or an extender's members
    # would never reach the mod.
    body = _frag_fn(loader, "function makeCtx(modId, rec) {")
    assert "const ctx = {" in body, "makeCtx must name the object it extends"
    assert body.index("_applyCtxExtenders(ctx, rec);") < body.index("return ctx;")
    # An extender is handed (ctx, rec) -- arguments, not closure: a companion
    # fragment shares the one <script> scope but NOT makeCtx's per-mod locals.
    assert "fn(ctx, rec);" in _frag_fn(
        ctx_ext, "function _applyCtxExtenders(ctx, rec) {")
    # Additive: a new ctx family does not move the contract version (a bump
    # would refuse every mod that pins v1).
    assert "ctxVersion: 1," in loader
    # And the seam reaches the served page, once.
    for sym in ("const _ctxExtenders = [];", "function _registerCtxExtender(fn) {",
                "_applyCtxExtenders(ctx, rec);"):
        assert sym in INDEX_HTML, f"#194 registry missing from served page: {sym!r}"
    # 86c ships as the place extenders are declared and registered, and says so.
    ext = _ctx_ext_src()
    assert "_registerCtxExtender(" in ext
    assert "ctxVersion" in ext, \
        "the extension fragment must state that new families stay additive"


def test_help_card_family_moved_out_of_the_loader_verbatim():
    # #194's relocation is a PURE move: the declarations left the loader and
    # landed in 86d unchanged, so the served page is unchanged too. Assert both
    # halves -- present there, gone from here -- or a copy could satisfy one of
    # them while the loader kept a stale duplicate (two `const _HELP_BLOCK_TYPES`
    # in one <script> is a SyntaxError that would blank the whole desktop).
    loader = _loader_src()
    cards = _help_cards_src()
    for sym in ("const _HELP_BLOCK_TYPES", "const _HELP_SPAN_TYPES",
                "function _sanitizeHelpSpan(", "function _sanitizeHelpBlock(",
                "function _sanitizeHelpBlocks(", "function _sanitizeHelpCard(",
                "function _refreshHelpIfOpen(", "function _modRegisterHelpCards("):
        assert sym in cards, f"{sym!r} did not land in 86d"
        assert sym not in loader, f"{sym!r} is still declared in the loader"
        assert INDEX_HTML.count(sym) == 1, \
            f"{sym!r} must appear exactly once in the served page"
    # The loader still CALLS into the family (setModEnabled / _applyPolicyLive
    # nudge an open Help window), which works because it is all one <script> --
    # the direction of the split does not matter to a hoisted function.
    assert "_refreshHelpIfOpen();" in loader
    # ctx.registerHelpCards itself stays on the ctx in the loader, wired to the
    # relocated implementation.
    assert "return _modRegisterHelpCards(rec, cards);" in loader


_CTX_EXT_SLICE_START = "// ---- ctx-extender registry (#194) ---"
_CTX_EXT_SLICE_END = "// ---- end ctx-extender registry ---"


def _ctx_registry_source():
    """The shipped ctx-extender registry range, verbatim. Declaration-only (an
    array + two functions), which is what makes it runnable outside a browser;
    the markers keep the range honest."""
    src = _ctx_ext_src()
    start = src.index(_CTX_EXT_SLICE_START)
    end = src.index(_CTX_EXT_SLICE_END)
    assert start < end, "slice markers out of order"
    body = src[start:end]
    for needed in ("const _ctxExtenders = [];",
                   "function _registerCtxExtender(fn) {",
                   "function _applyCtxExtenders(ctx, rec) {"):
        assert needed in body, f"{needed} missing from the sliced range"
    return body


_CTX_EXT_HARNESS = r"""
'use strict';
// The loader logs a failed extender through console.error, like every other
// per-mod failure. Capture it rather than letting it reach stderr, so a case
// can assert the message names the extender that threw.
const errors = [];
console.error = function () {
    errors.push(Array.prototype.map.call(arguments, String).join(' '));
};

__REGISTRY__

// ---- driver -------------------------------------------------------------
const log = [];
function mk(name, opts) {
    const fn = function (ctx, rec) {
        log.push(name);
        if (opts && opts.throws) throw new Error('boom from ' + name);
        ctx[name] = true;
        if (rec && rec.seen) rec.seen.push(name);
    };
    // A NAMED function is the convention the registry documents (its error
    // message reports fn.name), so the fixtures are named too.
    Object.defineProperty(fn, 'name', { value: name });
    return fn;
}
function rec() { return { id: 'fixture', unloads: [], seen: [] }; }

const CASES = {};

// Extenders run in registration order == fragment order.
CASES.order = function () {
    const added = [_registerCtxExtender(mk('alpha')),
                   _registerCtxExtender(mk('beta')),
                   _registerCtxExtender(mk('gamma'))];
    const ctx = { id: 'fixture' };
    const r = rec();
    const out = _applyCtxExtenders(ctx, r);
    return { log: log, added: added, keys: Object.keys(ctx),
             same: out === ctx, recSeen: r.seen, count: _ctxExtenders.length };
};

// One throwing extender takes neither its siblings nor ctx construction down,
// and does not poison the registry for the NEXT mod.
CASES.throwing_extender = function () {
    _registerCtxExtender(mk('alpha'));
    _registerCtxExtender(mk('bad', { throws: true }));
    _registerCtxExtender(mk('gamma'));
    const ctx = { id: 'first' };
    let threw = false;
    try { _applyCtxExtenders(ctx, rec()); } catch (_) { threw = true; }
    const second = { id: 'second' };
    _applyCtxExtenders(second, rec());
    return { log: log, threw: threw, keys: Object.keys(ctx),
             secondKeys: Object.keys(second), errors: errors };
};

// Registering the SAME function twice runs it once...
CASES.duplicate_registration = function () {
    const a = mk('alpha');
    const added = [_registerCtxExtender(a), _registerCtxExtender(a)];
    _registerCtxExtender(mk('beta'));
    const ctx = { id: 'fixture' };
    _applyCtxExtenders(ctx, rec());
    return { log: log, added: added, count: _ctxExtenders.length };
};

// ...and so does pushing it onto the array by hand, twice (the apply loop
// keeps only each function's FIRST occurrence, so order is the first one).
CASES.duplicate_raw_push = function () {
    const a = mk('alpha');
    _ctxExtenders.push(a);
    _ctxExtenders.push(mk('beta'));
    _ctxExtenders.push(a);
    const ctx = { id: 'fixture' };
    _applyCtxExtenders(ctx, rec());
    return { log: log, count: _ctxExtenders.length };
};

// A non-function is refused at registration, so it can never be called.
CASES.rejects_non_functions = function () {
    const added = [_registerCtxExtender(null), _registerCtxExtender(undefined),
                   _registerCtxExtender({}), _registerCtxExtender('nope'),
                   _registerCtxExtender(42)];
    const ctx = { id: 'fixture' };
    _applyCtxExtenders(ctx, rec());
    return { added: added, count: _ctxExtenders.length, keys: Object.keys(ctx) };
};

// With nothing registered, ctx construction is untouched -- the feature costs
// an empty loop until a fragment opts in.
CASES.registers_during_the_pass = function () {
    // An extender that registers ANOTHER extender mid-pass must not extend the
    // loop it is running in. Before the snapshot, appending a fresh identity
    // every call never terminated -- ctx construction hanging takes the
    // desktop with it.
    const log = [];
    let n = 0;
    function greedy(ctx) {
        log.push('greedy');
        // a NEW identity every time: the un-snapshotted loop would run forever
        _registerCtxExtender(function later() { n += 1; log.push('later' + n); });
    }
    _registerCtxExtender(greedy);
    const ctx = { id: 'm1' };
    _applyCtxExtenders(ctx, { unloads: [] });
    const firstPass = log.slice();
    // The registration DOES take effect for the next mod.
    const ctx2 = { id: 'm2' };
    _applyCtxExtenders(ctx2, { unloads: [] });
    return { firstPass: firstPass, secondPass: log.slice(firstPass.length),
             terminated: true };
};

CASES.throwing_report_surface = function () {
    // The failure REPORT must not become the second failure: fn.name and
    // ctx.id are attacker-adjacent reads. A throwing getter there used to
    // escape the loop and cost every remaining extender.
    const log = [];
    const bad = function () { log.push('bad'); throw new Error('boom'); };
    Object.defineProperty(bad, 'name', {
        get: function () { throw new Error('name explodes'); },
    });
    _registerCtxExtender(bad);
    _registerCtxExtender(function after(ctx) { log.push('after'); ctx.after = 1; });
    const ctx = {};
    Object.defineProperty(ctx, 'id', {
        get: function () { throw new Error('id explodes'); },
        enumerable: true,
    });
    let threw = false;
    try { _applyCtxExtenders(ctx, { unloads: [] }); } catch (_) { threw = true; }
    return { log: log, threw: threw, siblingRan: ctx.after === 1 };
};

CASES.empty_registry = function () {
    const ctx = { id: 'fixture' };
    const out = _applyCtxExtenders(ctx, rec());
    return { log: log, keys: Object.keys(ctx), same: out === ctx,
             count: _ctxExtenders.length, errors: errors };
};

const want = process.argv[2];
if (!CASES[want]) { console.log('no such case: ' + want); process.exit(2); }
const r = CASES[want]();
if (!('errors' in r)) r.errors = errors;
process.stdout.write(JSON.stringify(r) + '\n');
"""


@pytest.fixture(scope="module")
def ctx_ext_harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("ctxext") / "harness.js"
    path.write_text(_CTX_EXT_HARNESS.replace("__REGISTRY__",
                                             _ctx_registry_source()),
                    encoding="utf-8")
    return path


def _run_ctx_ext(harness, case):
    proc = subprocess.run([NODE, str(harness), case],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"case {case} failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_ctx_extenders_run_in_registration_order(ctx_ext_harness):
    r = _run_ctx_ext(ctx_ext_harness, "order")
    assert r["log"] == ["alpha", "beta", "gamma"], \
        "registration order is _ORDERED order -- a later fragment must see " \
        "what an earlier one put on the ctx"
    assert r["added"] == [True, True, True]
    assert r["count"] == 3
    # Each extender decorated the object makeCtx is building, in place, and the
    # apply hands that SAME object back (an extender cannot swap the ctx out).
    assert r["keys"] == ["id", "alpha", "beta", "gamma"]
    assert r["same"] is True
    # ...and each was handed the per-mod record as its second argument, which is
    # how a companion fragment reaches rec.unloads without seeing makeCtx's
    # locals.
    assert r["recSeen"] == ["alpha", "beta", "gamma"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_throwing_ctx_extender_does_not_take_its_siblings_down(ctx_ext_harness):
    r = _run_ctx_ext(ctx_ext_harness, "throwing_extender")
    # Every extender ran on the first ctx; the throw did not abort the pass.
    assert r["log"][:3] == ["alpha", "bad", "gamma"]
    assert r["threw"] is False, "a bad extender must never reach makeCtx's caller"
    # The failed one contributed nothing; its siblings still did.
    assert r["keys"] == ["id", "alpha", "gamma"]
    # ...and the registry is not poisoned: the NEXT mod's ctx gets the same
    # treatment rather than being skipped.
    assert r["log"] == ["alpha", "bad", "gamma", "alpha", "bad", "gamma"]
    assert r["secondKeys"] == ["id", "alpha", "gamma"]
    # Logged the way every other per-mod failure is, naming the extender.
    assert len(r["errors"]) == 2
    assert "[mods] ctx extender failed" in r["errors"][0]
    assert "bad" in r["errors"][0]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_registering_the_same_ctx_extender_twice_is_idempotent(ctx_ext_harness):
    r = _run_ctx_ext(ctx_ext_harness, "duplicate_registration")
    assert r["added"] == [True, False], "a repeat registration must be refused"
    assert r["count"] == 2
    assert r["log"] == ["alpha", "beta"], "the same function must run once"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_duplicate_raw_push_still_runs_the_extender_once(ctx_ext_harness):
    # The array is reachable, so identity-idempotence is enforced at the apply
    # loop as well as at the registrar -- a fragment that pushes by hand cannot
    # decorate a ctx twice.
    r = _run_ctx_ext(ctx_ext_harness, "duplicate_raw_push")
    assert r["count"] == 3, "the raw pushes really did land on the array"
    assert r["log"] == ["alpha", "beta"], \
        "only each function's FIRST occurrence runs, so order is unchanged too"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_ctx_extender_registry_refuses_non_functions(ctx_ext_harness):
    r = _run_ctx_ext(ctx_ext_harness, "rejects_non_functions")
    assert r["added"] == [False] * 5
    assert r["count"] == 0
    assert r["keys"] == ["id"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_empty_ctx_extender_registry_leaves_ctx_untouched(ctx_ext_harness):
    # Today's shipped state: 86c registers nothing yet, so ctx v1 is exactly
    # what it was before the seam existed.
    r = _run_ctx_ext(ctx_ext_harness, "empty_registry")
    assert r["count"] == 0
    assert r["log"] == []
    assert r["keys"] == ["id"]
    assert r["same"] is True
    assert r["errors"] == []


def test_mod_scripts_exist_on_disk_and_match_mods_dir():
    # _MODS drift guard: the declared mod scripts exist, and every *.js under
    # mods/ is declared (no orphan mod script silently absent from the page).
    for rel in ui._MODS:
        assert (BROKER_DIR / rel).is_file(), f"declared mod missing on disk: {rel}"
    on_disk = {p.relative_to(BROKER_DIR).as_posix()
               for p in (BROKER_DIR / "mods").rglob("*.js")}
    declared = set(ui._MODS)
    assert on_disk == declared, (
        f"mods/ *.js drift: declared={sorted(declared)} on_disk={sorted(on_disk)}")


def test_mod_catalog_matches_the_registerMod_declarations():
    # #157: GET /info advertises ui.mod_catalog() so another broker's Control
    # Panel tab can list this broker's mods and label its policy's "Default"
    # option correctly. `defaultEnabled` and `requires` therefore live in BOTH
    # mod.json (read by Python) and the registerMod() call (read by the loader),
    # and this is the guard that keeps the copies honest -- in both directions,
    # so neither adding nor removing a declaration can drift silently.
    #
    # The JS is matched by source text on purpose: it is the same assertion style
    # test_clock_mod_packaged_and_manifest_agrees already uses, and the loader's
    # own contract is `defaultEnabled !== false` / `requires || []`, i.e. absent
    # means default-on / no deps.
    catalog = {m["id"]: m for m in ui.mod_catalog()}
    assert len(catalog) == len(ui._mod_dirs(ui._MODS)), "duplicate mod id"
    for mod_dir in ui._mod_dirs(ui._MODS):
        mid = PurePosixPath(mod_dir).name
        # id agreement: directory name == manifest id == the id registerMod
        # claims. All three are the pin/pane key namespace, so a mismatch would
        # let a policy pin name a mod the loader cannot resolve.
        assert mid in catalog, f"{mod_dir}: catalog id != directory name"
        src = "".join((BROKER_DIR / rel).read_text(encoding="utf-8")
                      for rel in ui._MODS
                      if PurePosixPath(rel).parent.as_posix() == mod_dir)
        assert f"id: '{mid}'" in src, f"{mod_dir}: registerMod id != dir name"

        js_default_off = re.search(r"defaultEnabled:\s*false", src) is not None
        assert catalog[mid]["default_enabled"] is not js_default_off, (
            f"{mod_dir}: mod.json defaultEnabled disagrees with its registerMod "
            f"declaration (JS says default-{'off' if js_default_off else 'on'})")

        js_requires = re.search(r"requires:\s*\[([^\]]*)\]", src)
        js_deps = sorted(re.findall(r"'([^']+)'", js_requires.group(1))) \
            if js_requires else []
        assert sorted(catalog[mid]["requires"]) == js_deps, (
            f"{mod_dir}: mod.json requires {catalog[mid]['requires']} but its "
            f"registerMod declares {js_deps}")
        # A declared dependency must be a mod that exists and is spliced EARLIER
        # (the loader's static ordering guard the pin resolver relies on).
        for dep in js_deps:
            assert dep in catalog, f"{mod_dir}: requires unknown mod {dep!r}"
            assert ui._mod_dirs(ui._MODS).index(f"mods/{dep}") \
                < ui._mod_dirs(ui._MODS).index(mod_dir), \
                f"{mod_dir}: dependency {dep!r} must load first"


def test_mod_catalog_ids_are_valid_policy_keys():
    # A catalog id doubles as a mod-policy key in the /state settings blob, and
    # app.py only reports policy keys matching the mod-id shape -- an id outside
    # it could never be pinned. Kept as the same regex /mod-store/<modId> uses.
    from webterm.broker.app import (_INSTALLED_ID_PREFIX, _MODSTORE_ID_RE,
                                    _is_reserved_mod_id)
    for m in ui.mod_catalog():
        assert _MODSTORE_ID_RE.fullmatch(m["id"]), \
            f"mod id {m['id']!r} cannot be used as a mod-policy key"
        # #172: the "x-" prefix is RESERVED for runtime-installed mods, so no
        # shipped mod may claim one. Without this a later shipped mod could
        # collide with an id somebody already installed -- inheriting its pins,
        # its /mod-store value and its webterm:mod:<id>:* localStorage keys,
        # which is the exact failure #172 describes.
        assert not m["id"].startswith(_INSTALLED_ID_PREFIX), (
            f"shipped mod id {m['id']!r} claims the reserved installed-mod "
            f"prefix {_INSTALLED_ID_PREFIX!r}")
        assert _is_reserved_mod_id(m["id"]), \
            f"shipped mod id {m['id']!r} is not in the first-party namespace"


def test_no_shipped_mod_requires_an_installed_mod_id():
    # #172/#163: a shipped mod may never depend on an "x-" (runtime-installed)
    # id. It keeps the shipped->installed dependency edge UNREPRESENTABLE, which
    # is what lets the catalog emit every shipped row first and lets the shipped
    # set keep its cheap positional ordering rule while the installed set is
    # sorted topologically at runtime. A shipped mod that needed an installed one
    # would also be broken on any broker that had not installed it.
    from webterm.broker.app import _INSTALLED_ID_PREFIX
    for m in ui.mod_catalog():
        for dep in m["requires"]:
            assert not dep.startswith(_INSTALLED_ID_PREFIX), (
                f"shipped mod {m['id']!r} requires {dep!r}, which is in the "
                f"runtime-installed namespace")


def test_clock_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "clock"
    js = mod_dir / "clock.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "clock"
    assert meta["ctxVersion"] == 1
    # The script registers the same id/ctxVersion the manifest declares.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'clock'" in src
    assert "ctxVersion: 1" in src


def test_set_mods_mount_and_loader_api_present():
    # The Control Panel mount point for mod-contributed settings, plus the public
    # loader API the mods + tests depend on, are in the served page.
    assert 'id="set-mods"' in INDEX_HTML
    for sym in ("function registerMod", "function loadMods",
                "function notifyModSettings", "function localInfo",
                "renderModSettingsToggles", "window.__mods"):
        assert sym in INDEX_HTML, f"missing loader symbol: {sym!r}"


def test_settings_extension_api_present():
    # #74 (S1): the generalized Control Panel settings-extension surface — radio,
    # select, and a full custom registerSettingsPane — rides in the served loader
    # alongside the unchanged boolean. These are the symbols mods (S2/S3/S5) and
    # the Playwright acceptance depend on.
    for sym in (
        "registerSettingsPane: function",
        "function _modSettingChoice",
        "function _modRegisterPane",
        "function _controlSection",
        "function _normChoiceOptions",
        "function _modSettingText",
    ):
        assert sym in INDEX_HTML, f"missing settings-extension symbol: {sym!r}"
    # ctx.settings now exposes radio/select/combo/text next to the unchanged
    # boolean.
    for sym in ("boolean: function", "radio: function", "select: function",
                "combo: function", "text: function"):
        assert sym in INDEX_HTML, f"missing ctx.settings widget: {sym!r}"
    # The #set-mods host is no longer itself browser-global (visibility is now
    # per-mounted-section, driven by each control's isBrowserGlobal opt), so a
    # non-global mod control can show on a remote host tab.
    assert '<div class="set-section" id="set-mods"></div>' in INDEX_HTML


def test_settings_text_primitive_contract():
    # #168: ctx.settings.text is the only settings primitive with no domain, and
    # the only NEW writer on _valueAccessor -- the single path into the synced
    # blob. UI JS never executes in CI, so its four load-bearing rules are locked
    # by source assertion, the same way #169's are.
    loader = _loader_src()
    text = _text_src()
    body = _frag_fn(text, "function _modSettingText(rec, key, opts) {")
    # The primitive is its own fragment, spliced into the SAME <script> right
    # after the loader (so it shares scope with _controlSection / _trackControl
    # / _valueAccessor) and before the mod scripts that call it.
    assert ui._ORDERED.index("86_js_mod_loader.js") + 1 \
        == ui._ORDERED.index("86a_js_mod_settings_text.js")
    assert ui._ORDERED.index("86a_js_mod_settings_text.js") \
        < ui._ORDERED.index(ui._MOD_SPLICE_BEFORE)

    # 1. READ-THROUGH IS STRUCTURAL AND NON-DESTRUCTIVE. read() gates a stored
    #    (or a peer's) value on shape alone and never writes -- so a value that
    #    no longer passes the mod's validator is NOT destroyed, it is simply not
    #    shown. That is the whole clockTz fix: validate on write only.
    assert "return _modTextOk(v, max) ? v : fallback;" in body
    ok = _frag_fn(text, "function _modTextOk(v, max) {")
    assert "userValidate" not in ok and "validate" not in ok, \
        "read()'s gate must be structural -- a domain check here evaporates a " \
        "value that was legal under an older validator / another engine"
    assert "getSettings()[key] =" not in body, \
        "only _valueAccessor.set may write the blob"

    # 2. COERCE + A HARD CAP. String -> strip control chars -> trim -> cap, the
    #    treatment core gives startLabel/startPath, and a ceiling no mod can
    #    raise. The cap counts UTF-16 code units -- the SAME unit mod-sync's
    #    STR_MAX counts -- so anything storable survives a cross-broker carry.
    consts = (BROKER_DIR / "50_js_constants.js").read_text(encoding="utf-8")
    assert "const MAX_MOD_TEXT_LEN = 1024;" in consts
    assert re.search(r"const MAX_MOD_POLICY_KEYS[^\n]*\n(?:\s*//[^\n]*\n)*"
                     r"\s*const MAX_MOD_TEXT_LEN", consts), \
        "the text cap belongs beside MAX_MOD_POLICY_KEYS"
    sync_src = (BROKER_DIR / "mods" / "mod-sync" / "mod-sync.js").read_text(
        encoding="utf-8")
    str_max = int(re.search(r"const STR_MAX = (\d+);", sync_src).group(1))
    text_max = int(re.search(r"const MAX_MOD_TEXT_LEN = (\d+);", consts).group(1))
    assert text_max < str_max, \
        f"MAX_MOD_TEXT_LEN {text_max} must sit under mod-sync's STR_MAX {str_max}"
    coerce = _frag_fn(text, "function _modTextCoerce(v, max) {")
    assert "try { s = (v == null) ? '' : String(v); } catch (_) { return ''; }" \
        in coerce, "a throwing toString must not escape a settings write"
    assert r"_modTextDropLone(s.replace(/[\u0000-\u001F\u007F]/g, '')).trim();" \
        in coerce
    assert "if (s.length > max) s = _modTextDropLone(s.slice(0, max)).trim();" \
        in coerce
    assert "Math.min(Math.floor(opts.maxLength), MAX_MOD_TEXT_LEN)" in body, \
        "a mod may ask for LESS than the ceiling, never more"
    # Well-formedness is part of the STRUCTURE, not a nicety. Cutting at a
    # code-unit boundary can split a surrogate PAIR, and a peer can send an
    # already-broken one; a lone surrogate survives JSON only as an escape and
    # could never be retyped, so coerce drops it and the gate refuses it.
    # Hand-rolled because the regex forms need lookbehind (ES2018) or
    # isWellFormed (ES2024) -- and an old engine is this feature's whole point.
    paired = _frag_fn(text, "function _modTextPaired(s) {")
    assert "if (c > 0xDBFF) return false;" in paired
    assert "if (!(n >= 0xDC00 && n <= 0xDFFF)) return false;" in paired
    assert "return _modTextPaired(v);" in ok
    assert "lookbehind" in text and "isWellFormed" in text

    # 3. A VISIBLE REJECTION, reusing the error affordance that already exists
    #    (.set-err / .set-err.show, 15_css_dialogs.css) rather than inventing a
    #    second one -- and the rejected draft STAYS on screen, because a silent
    #    drop is what made this user-hostile in the first place.
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    assert ".set-err {" in css and ".set-err.show {" in css
    assert "err.className = 'set-err';" in body
    assert "err.classList.add('show');" in body
    # A validator that throws is caught and fails CLOSED -- it must never
    # propagate out of savePrefs, and it must not be trusted either.
    assert "catch (e) {" in body and "validate threw" in body
    # The verdict is recorded by valid() -- the one place that sees the coerced
    # value the shared writer judges -- so commit() can show it without the
    # shared writer growing a rejection path of its own.
    assert "function (v) { rejectMsg = check(v); return rejectMsg === ''; }," \
        in body
    assert "if (rejectMsg) setErr(rejectMsg);" in body
    # An `async` validator returns a PROMISE, which is truthy: under the
    # forgiving "anything else accepts" rule it would wave every value through.
    # Fail closed on a thenable, loudly.
    assert "if (r && typeof r.then === 'function') {" in body
    # A rejected draft must NOT gate reflect(). notifyModSettings sets
    # entry.last BEFORE reflecting, so one skip is permanent -- every later poll
    # sees cur === last and never retries, stranding the box on a draft that
    # will never be stored while the mod has already moved on.
    ref = body[body.index("function reflect() {"):]
    ref = ref[:ref.index("\n            }")]
    assert "if (drafting) return;" not in ref, \
        "a draft must not permanently mask convergence"
    assert "setErr('');" in ref and "input.value = read();" in ref

    # 4. DEBOUNCE, FLUSHED BOTH WAYS. Core's start-path cadence: 400 ms on
    #    'input', flush on 'change'. Plus a flush on TEARDOWN -- the value
    #    outlives the mod, so a keystroke inside the window is data loss.
    assert "timer = setTimeout(commit, 400);" in body
    assert "input.addEventListener('change', function () {" in body
    assert "rec.unloads.push(function () { if (timer) commit(); });" in body
    # ...and that flush is pushed AFTER _trackControl, so the LIFO drain runs it
    # FIRST, while the entry is still tracked (entry.last stays truthful).
    assert body.index("_trackControl(rec, entry);") \
        < body.index("rec.unloads.push(function () { if (timer) commit(); });")
    # Teardown cannot cover a reload / tab close inside the 400 ms window.
    # savePrefs writes localStorage synchronously, so a pagehide flush keeps the
    # keystroke even when the /state PUT never leaves. Removed with the mod.
    assert "window.addEventListener('pagehide', onPageHide);" in body
    assert "window.removeEventListener('pagehide', onPageHide);" in body

    # Combo's two hard-won bits, kept: the focus guard (a /state convergence must
    # not clobber an in-progress edit) and the blur reconcile (an edit that ends
    # with no change event must not leave a remote value stale).
    assert "if (document.activeElement === input) return;" in body
    blur = body[body.index("input.addEventListener('blur', function () {"):]
    blur = blur[:blur.index("\n            });")]
    # The blur reconcile is a DATA-LOSS path unless it flushes first: with a
    # commit still pending it would put the OLD stored value in the box and the
    # timer would then commit that, silently reverting the edit. 'change'
    # normally fires first and clears the timer -- "normally" is not a contract
    # worth betting an edit on (a programmatic blur, a hidden section).
    assert blur.index("if (timer) commit();") < blur.index("input.value = read();")

    # 5. THE NO-OP TEST IS THE RAW VALUE, not read(). read() answers with the
    #    FALLBACK for a structurally broken stored value, so the shared writer's
    #    default read()-equality would make "clear the box" indistinguishable
    #    from a no-op against exactly the junk that most needs clearing -- and
    #    leave it in the synced blob, re-pushed by every savePrefs.
    setter = _loader_fn("function _valueAccessor(")
    assert "function _valueAccessor(entry, key, read, coerce, valid, unchanged) {" \
        in loader
    assert "if (unchanged ? unchanged(value) : (read() === value)) return;" \
        in setter, "omitting `unchanged` must be byte-for-byte the old behaviour"
    assert "return raw === v || (raw === undefined && v === fallback);" in body, \
        "with nothing stored, committing the default must still write nothing"
    # Only text passes it: the other two call sites still close on their `valid`
    # argument, so they keep read()-equality untouched.
    assert "function () { return true; });          // valid (any boolean)" \
        in loader
    assert "function (v) { return valid[v] === true; });" in loader
    # The mutable state is function-LOCAL, never a fragment-level let/const: a
    # hoisted function reading a not-yet-initialized fragment `let` throws a TDZ
    # ReferenceError that disables the whole mod, and CI never runs this JS.
    for banned in ("\n        let _modText", "\n        const _MOD_TEXT",
                   "\n        let _textDraft"):
        for frag in (loader, text):
            assert banned not in frag, \
                f"{banned.strip()!r} is a TDZ hazard here"
    # ctx surface + the entry shape mod-sync reads.
    assert "text: function (key, opts) {" in loader
    assert "return _modSettingText(rec, key, opts);" in loader
    assert "kind: 'text', key: key, read: read," in body
    assert "maxLength: max," in body


def test_mod_sync_accepts_the_text_kind():
    # #168's mandatory companion: acceptedBy's DOM scrape cannot answer for a
    # kind with no option set, and its default `return true` would plant a value
    # read() then ignores -- the exact failure that function exists to prevent.
    # The honest answer is read()'s own STRUCTURAL gate, taken from the loader so
    # the two can never drift, and deliberately domain-free: a zone the sending
    # engine knows and this one does not must still carry (that IS the bug).
    src = (BROKER_DIR / "mods" / "mod-sync" / "mod-sync.js").read_text(
        encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    assert "if (entry.kind === 'text') {" in code
    assert "return _modTextOk(v, entry.maxLength);" in code
    # Guarded like _localPin: a build without the predicate degrades to the
    # scalar bound rather than throwing inside the adopt preview.
    assert "if (typeof _modTextOk === 'function') {" in code
    assert "return typeof v === 'string' && v.length <= STR_MAX;" in code
    # And it is answered BEFORE the select/radio scrape, which would return
    # false for every text value (a text section has no <option> of its own
    # unless the mod supplied suggestions).
    assert code.index("if (entry.kind === 'text') {") \
        < code.index("if (entry.kind === 'select' || entry.kind === 'radio') {")


def test_clock_symbols_removed_from_core_fragments():
    # The clock is now a mod: its core renderer/handlers/markup are gone. Scope
    # the check to the CORE fragments it was extracted from (the mod script
    # legitimately still names clock-chip / the `clock` key).
    core = {
        "65_js_display_theming.js": ("applyClock", "_renderClock", "_clockTimer"),
        "40_body.html": ('id="clock-chip"', 'id="set-clock"'),
        "11_css_apps.css": ("#clock-chip",),
        "79_js_settings_modal.js": ("setClockEl",),
        "81_js_control_panel.js": ("setClockEl",),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


# --------------------------------------------------------------------------- #
# theme mod (#75 / S2)
# --------------------------------------------------------------------------- #

def test_theme_symbols_removed_from_core_fragments():
    # The color scheme is now a mod (#75): its THEMES palette / labels /
    # applyTheme, the #set-theme radio markup + CSS, the core normalization, and
    # its Control Panel reflect/handler are gone from core. Scope the check to the
    # CORE fragments it was extracted from (the mod script legitimately still
    # names THEMES / applyTheme / the `theme` key). applyThemeSettings (the still-
    # core convergence entry point) deliberately survives — the sentinels below
    # are specific enough not to match it.
    core = {
        "65_js_display_theming.js": ("const THEMES", "THEME_LABELS", "applyTheme(name)"),
        "55_js_settings_model.js": ("hasOwnProperty.call(THEMES",),
        "40_body.html": ('id="set-theme"',),
        "15_css_dialogs.css": ("#set-theme",),
        "79_js_settings_modal.js": ("setThemeEl", "Object.keys(THEMES)"),
        "81_js_control_panel.js": ("setThemeEl", "applyTheme("),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_theme_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "theme"
    js = mod_dir / "theme.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "theme"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "theme.js"
    # The script registers the theme mod, owns the synced `theme` key through the
    # #74 radio API, and carries the moved palette + apply function.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'theme'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.settings.radio('theme'" in src
    assert "const THEMES" in src
    assert "function applyTheme(name)" in src
    # And the mod ships in the served page (present in the mod / gone from core).
    assert "ctx.settings.radio('theme'" in INDEX_HTML
    assert "id: 'theme'" in INDEX_HTML
    # The default stays night: it is the first option, the radio's `def`, and
    # still equals the :root CSS, so the visual default survives a pre-load paint.
    assert "def: 'night'" in src


# --------------------------------------------------------------------------- #
# semantic status palette (#173)
# --------------------------------------------------------------------------- #

# The historical ok / warn / danger literals, which #173 replaced with the
# --ok / --warn / --danger vars. `night` must keep rendering exactly these.
_STATUS_HISTORICAL = {"ok": "#5fbf7f", "warn": "#e0a93a", "danger": "#e96d6d"}


def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _strip_css_comments(text):
    import re
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_status_vars_defined_as_theme_derived_mixes():
    # #173: the status palette is three color-mix() derivations on :root, beside
    # --sel-bg, so it recolors with the theme instead of being a dark-theme hex
    # copy-pasted across core and four mods.
    import re
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    for role in _STATUS_HISTORICAL:
        m = re.search(
            r"--%s:\s*color-mix\(in srgb, (#[0-9a-fA-F]{6}) (\d+)%%, var\(--fg\)\);"
            % role, css)
        assert m, f"--{role} must be a color-mix(... var(--fg)) derivation on :root"
    assert "--ok:" in INDEX_HTML and "--warn:" in INDEX_HTML \
        and "--danger:" in INDEX_HTML, "the status vars must reach the served page"


def test_status_vars_round_trip_to_the_historical_night_palette():
    # The base hexes are PRE-COMPENSATED for the mix against `night`'s --fg, so
    # the default theme is unchanged at 8-bit. That is the whole reason the bases
    # look wrong to the naked eye, so lock it: editing the percentage, a base, or
    # night's --fg must fail HERE rather than silently restyle every status color
    # in the app. color-mix(in srgb, ...) is a plain lerp of the gamma-encoded
    # channels (both inputs opaque), which is what this reproduces.
    import re
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    theme = (BROKER_DIR / "mods" / "theme" / "theme.js").read_text(encoding="utf-8")

    night = re.search(r"night:.*?'--fg':\s*'(#[0-9a-fA-F]{3,6})'", theme, re.S)
    assert night, "could not read night's --fg out of the theme mod"
    fg = _hex_to_rgb(night.group(1))

    for role, historical in _STATUS_HISTORICAL.items():
        m = re.search(
            r"--%s:\s*color-mix\(in srgb, (#[0-9a-fA-F]{6}) (\d+)%%, var\(--fg\)\);"
            % role, css)
        base, pct = _hex_to_rgb(m.group(1)), int(m.group(2)) / 100.0
        mixed = tuple(round(pct * base[i] + (1 - pct) * fg[i]) for i in range(3))
        want = _hex_to_rgb(historical)
        # warn's raw base clips the blue floor at 0, so it lands 4/255 off — far
        # below the just-noticeable threshold, but pin the tolerance so a real
        # drift cannot hide behind it.
        for i in range(3):
            assert abs(mixed[i] - want[i]) <= 4, (
                f"--{role} resolves to {mixed} on night, but the historical color "
                f"is {want} ({historical}); the default theme would visibly shift")


def test_status_literals_not_re_hardcoded_in_any_stylesheet():
    # The point of #173: adding/changing a status color must be a one-line edit,
    # not seven. Comments are stripped first -- 10_css_root.css documents the
    # historical values on purpose. 50_js_constants.js is deliberately NOT in
    # scope: its #5fbf7f / #e96d6d are entries in PALETTE, the per-window ACCENT
    # picker, which is persisted and parsed by hexToRgb()/isDarkAccent(), so a
    # var() string there would be a NaN, not a color.
    for name in (*(n for n in ui._ORDERED if n.endswith(".css")),
                 *_declared_mod_css()):
        text = _strip_css_comments(
            (BROKER_DIR / name).read_text(encoding="utf-8"))
        for role, literal in _STATUS_HISTORICAL.items():
            assert literal not in text.lower(), (
                f"{name} hardcodes {literal}; use var(--{role}) instead")


def test_aistatus_chip_bands_use_the_status_vars():
    # The one real JS call site (#173): the aistatus taskbar chip sets its color
    # from an inline style, so it must hand the CSSOM a var() string -- exactly
    # what the pre-existing grey band already does with var(--bg-3)/var(--fg-dim).
    src = (BROKER_DIR / "mods" / "aistatus" / "aistatus.js").read_text(
        encoding="utf-8")
    for var in ("var(--ok)", "var(--warn)", "var(--danger)"):
        assert var in src, f"aistatus BANDS should carry {var}"
    for literal in _STATUS_HISTORICAL.values():
        assert literal not in src.lower(), (
            f"aistatus.js still hardcodes {literal}")


def test_aistatus_renders_broker_gate_as_disabled_not_provider_down():
    # #189: /status/fetch grew an operator gate that answers 503
    # {"ok": false, "error": "status_fetch_disabled"} when the broker was
    # never opted in. The client must recognize that exact code on the
    # response it already makes (zero new requests) and render it as the
    # BROKER'S choice -- never as a provider outage, and never by probing
    # a route ahead of time.
    src = (BROKER_DIR / "mods" / "aistatus" / "aistatus.js").read_text(
        encoding="utf-8")
    assert "status_fetch_disabled" in src, \
        "aistatus.js must recognize the status_fetch_disabled gate code"
    assert "r.status === 503" in src, \
        "aistatus.js must branch on the 503 the gate answers with"
    assert "switched off on this" in src, \
        "aistatus.js should name the broker as the one that switched checks off"
    # The poll loop must keep running while disabled -- an operator grant
    # heals the very next tick, no reload. poll() itself must never stop
    # the timer on the disabled branch (only ctx.onUnload's teardown does).
    assert "if (lastDisabled)" in src
    poll_fn = src[src.index("async function poll()"):
                  src.index("// ---- app window")]
    assert "stop(" not in poll_fn, (
        "poll() must not stop the timer when the broker gate is closed -- "
        "the poll loop has to keep ticking so a grant heals it")
    # #189's GUI grant path: the disabled note carries the switch it
    # promises -- one NAMED-direction write to the serving broker's own
    # consent seam, success re-polls immediately, and a config-pinned key
    # renders the operator's file as the decider (409 policy_locked).
    assert "Switch on status checks on this broker" in src
    grant_fn = src[src.index("async function grantStatusChecks"):
                   src.index("async function poll()")]
    assert "JSON.stringify(" in grant_fn and \
        "{ status_fetch_enabled: true }" in grant_fn, \
        "the grant write must NAME its direction (#187's rule)"
    assert "policy_locked" in grant_fn
    assert '"status_fetch_enabled", so that file ' in grant_fn
    assert "await poll();" in grant_fn, \
        "a successful grant must repaint immediately, not wait out the tick"
    assert "hostFetch(localHost()" in grant_fn, \
        "the write goes to the SERVING broker, explicitly"


# --------------------------------------------------------------------------- #
# pattern mod (#76 / S3)
# --------------------------------------------------------------------------- #

def test_pattern_symbols_removed_from_core_fragments():
    # The desktop background pattern is now a mod (#76): its PATTERNS list /
    # labels / the theme-var-aware applyPattern painter, the #set-pattern <select>
    # markup, the core normalization, and its Control Panel reflect/handler are
    # gone from core. Scope the check to the CORE fragments it was extracted from
    # (the mod script legitimately still names PATTERNS / applyPattern / the
    # `pattern` key; comments may still mention the word "pattern"). The sentinels
    # are specific enough not to match the surviving prose.
    core = {
        "65_js_display_theming.js": ("const PATTERNS", "PATTERN_LABELS", "function applyPattern"),
        "55_js_settings_model.js": ("PATTERNS.indexOf",),
        "40_body.html": ('id="set-pattern"',),
        "79_js_settings_modal.js": ("setPatternEl", "of PATTERNS"),
        "81_js_control_panel.js": ("setPatternEl", "applyPattern("),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_pattern_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "pattern"
    js = mod_dir / "pattern.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "pattern"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "pattern.js"
    # The script registers the pattern mod, owns the synced `pattern` key through
    # the #74 select API, and carries the moved list + labels + painter.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'pattern'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.settings.select('pattern'" in src
    assert "const PATTERNS" in src
    assert "function applyPattern" in src
    # The default stays none: it is the first option AND the select's `def`, and
    # applyPattern('none') clears the inline background, so the visual default is
    # preserved with no core normalization.
    assert "def: 'none'" in src
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the theme mod so notifyModSettings writes the chrome vars
    # before the pattern repaints on a both-changed /state pull, and applyPattern
    # is a hoisted global the theme mod's coupling can still reach.
    assert "ctx.settings.select('pattern'" in INDEX_HTML
    assert "id: 'pattern'" in INDEX_HTML
    assert "function applyPattern" in INDEX_HTML
    assert INDEX_HTML.index("id: 'theme'") < INDEX_HTML.index("id: 'pattern'")


# --------------------------------------------------------------------------- #
# termfont mod (#126)
# --------------------------------------------------------------------------- #

# The baseline monospace stack core constructs terminals with (67) MUST equal the
# termfont mod's TERM_FONT_DEFAULT (the family the mod resets to on disable). Both
# fragments carry this exact literal; the parity check below guards the coupling.
_TERM_FONT_BASELINE_LITERAL = "'Consolas, \"Liberation Mono\", monospace'"


def test_termfont_symbols_removed_from_core_fragments():
    # The terminal font is now a mod (#126): its TERM_FONTS list, terminalFontFamily,
    # the applyTerminalFont painter, the #set-term-font markup, the core
    # normalization, its Control Panel option-population + reflect + change handler,
    # and the construction-time reader are all gone from core. Scope the check to the
    # CORE fragments it was extracted from (the mod script + surviving core PROSE
    # legitimately still name the symbols); the sentinels are the actual CODE, chosen
    # specific enough not to match the comments that remain.
    core = {
        "65_js_display_theming.js": (
            "const TERM_FONTS", "function terminalFontFamily",
            "function applyTerminalFont",
        ),
        "55_js_settings_model.js": ("TERM_FONTS.some",),
        "40_body.html": ('id="set-term-font"',),
        "79_js_settings_modal.js": ("setTermFontEl", "of TERM_FONTS"),
        "81_js_control_panel.js": ("setTermFontEl",),
        # The construction-time reader is replaced by a self-contained baseline; no
        # mod symbol survives in the terminal factory.
        "67_js_window_lifecycle.js": ("terminalFontFamily",),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # Core constructs terminals with its own baseline (the decoupling), and that
    # literal MUST match the mod's TERM_FONT_DEFAULT so a disabled mod's terminals
    # land on the same font as a fresh core-only terminal. Parse the ACTUAL const
    # assignments (not mere literal presence) and assert they are byte-equal, so a
    # dead string / comment can't satisfy the coupling and a real drift is caught.
    import re
    core67 = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "fontFamily: TERM_FONT_BASELINE" in core67, \
        "core must construct terminals with the baseline, not a mod symbol"
    mod_src = (BROKER_DIR / "mods" / "termfont" / "termfont.js").read_text(encoding="utf-8")
    m_core = re.search(r"const\s+TERM_FONT_BASELINE\s*=\s*('[^']*'|\"[^\"]*\");", core67)
    m_mod = re.search(r"const\s+TERM_FONT_DEFAULT\s*=\s*('[^']*'|\"[^\"]*\");", mod_src)
    assert m_core, "core 67 must assign const TERM_FONT_BASELINE = <string literal>"
    assert m_mod, "the mod must assign const TERM_FONT_DEFAULT = <string literal>"
    assert m_core.group(1) == m_mod.group(1) == _TERM_FONT_BASELINE_LITERAL, (
        "core TERM_FONT_BASELINE and mod TERM_FONT_DEFAULT must be the SAME literal; "
        f"core={m_core.group(1)!r} mod={m_mod.group(1)!r}")


def test_termfont_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "termfont"
    js = mod_dir / "termfont.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "termfont"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "termfont.js"
    assert "mods/termfont/termfont.js" in ui._MODS
    src = js.read_text(encoding="utf-8")
    # Registers the termfont mod, default-OFF, with the reviewed tiers, owning the
    # synced `termFont` key through the #74 select API + the moved list/painter.
    assert "registerMod(" in src
    assert "id: 'termfont'" in src
    assert "ctxVersion: 1" in src
    assert "defaultEnabled: false" in src
    assert "tiers: ['settings', 'window']" in src
    assert "ctx.settings.select('termFont'" in src
    assert "const TERM_FONTS" in src
    assert "function applyTerminalFont" in src
    # The default stays the built-in (empty value): it is the first option AND the
    # select's `def`, so the visual default survives with no core normalization.
    assert "def: ''" in src
    # Rides the per-terminal-window hook, feature-detected — NOT a construction-time
    # core read (that decoupling is the whole point); tears down on disable.
    assert "if (!ctx.windows) return;" in src
    assert "ctx.windows.onTerminalCreate(" in src
    assert "ctx.onUnload(" in src
    # And the mod ships in the served page (present in the mod / gone from core),
    # appended AFTER the scratchpad mod (last in _MODS).
    assert "ctx.settings.select('termFont'" in INDEX_HTML
    assert "id: 'termfont'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'scratchpad'") < INDEX_HTML.index("id: 'termfont'")


def test_clock_tz_selector_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "clock"
    js = mod_dir / "clock.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "clock"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "clock.js"
    # #104: the clock owns a synced `clockTz` time-zone key (browser-global,
    # def '' == follow the viewing browser). The zone list is built dynamically
    # from Intl.supportedValuesOf with a curated fallback (Asia/Tokyo is one of
    # the fallback markers). The mod declares the `settings` tier on top of
    # `taskbar` (order must match _EXPECTED_TIERS).
    #
    # #168: it is ctx.settings.TEXT, not combo. That list is engine-dependent
    # (~418 zones, or 15 without supportedValuesOf) and combo treats its list as
    # the legal DOMAIN, so a zone picked in one browser evaporated in another;
    # text takes the list as SUGGESTIONS and validates on write against the
    # engine itself, which is the same authority render() already asks.
    src = js.read_text(encoding="utf-8")
    for needle in ("registerMod(", "id: 'clock'", "ctxVersion: 1",
                   "tiers: ['taskbar', 'settings']",
                   "ctx.settings.text('clockTz'", "def: ''",
                   "options: tzOptions", "placeholder: '(browser default)'",
                   "new Intl.DateTimeFormat(undefined, { timeZone: v })",
                   "Intl.supportedValuesOf", "Asia/Tokyo"):
        assert needle in src, f"missing clock-tz sentinel in mod src: {needle!r}"
    # combo's option list was a domain, so it had to carry an ''-valued entry to
    # make "follow this browser" selectable. text's placeholder says it instead —
    # a stray empty option would put a blank row in the datalist.
    assert "value: ''" not in src, \
        "clock's zone list must be suggestions only, with no empty-value option"
    # And it ships in the served page — the mod script + the datalist-backed
    # input the text primitive builds for its suggestions.
    for needle in ("ctx.settings.text('clockTz'", "def: ''",
                   "(browser default)", "Intl.supportedValuesOf", "Asia/Tokyo",
                   "createElement('datalist')"):
        assert needle in INDEX_HTML, f"missing clock-tz sentinel in page: {needle!r}"


# --------------------------------------------------------------------------- #
# help mod (#78 / S5)
# --------------------------------------------------------------------------- #

def test_help_symbols_removed_from_core_fragments():
    # The Help WINDOW, the taskbar "?" chip, the show/hide toggle, the chip
    # wiring and the render machinery are now a mod (#78): their core markup /
    # handlers / CSS are gone. Scope the check to the CORE fragments they were
    # extracted from. The corpus DATA pipeline (fetchHelpCorpus / buildHelpEntries
    # / /help-corpus.json) deliberately STAYS in core 80 (it reads core state), so
    # the sentinels target only the moved window/chip/toggle, never the kept
    # corpus (see test_help_corpus_pipeline_kept_in_core).
    core = {
        "65_js_display_theming.js": ("applyHelpButton",),
        "40_body.html": ('id="help-chip"', 'id="set-help-button"'),
        "12_css_help.css": ("#help-chip", ".app-help"),
        "79_js_settings_modal.js": ("setHelpButtonEl",),
        "81_js_control_panel.js": ("setHelpButtonEl", "function focusOrOpenHelp",
                                   "wireHelpChip", "maybeShowHelpHint",
                                   "applyHelpButton"),
        "80_js_help_window.js": ("function openHelpWindow", "function buildHelpBody",
                                 "function renderHelpInto", "function findHelpWindow"),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"


def test_help_corpus_pipeline_kept_in_core():
    # Issue #78 keeps the corpus + the buildHelpEntries merge in core (they read
    # core state: KEY_ACTIONS / profilesCache / mcpConfigCache); the help mod
    # calls these hoisted functions. They must NOT have been swept into the mod.
    src = (BROKER_DIR / "80_js_help_window.js").read_text(encoding="utf-8")
    for sym in ("function buildHelpEntries", "function fetchHelpCorpus",
                "function flattenHelpCorpus", "function helpTextBlock"):
        assert sym in src, f"{sym!r} must stay in core 80_js_help_window.js"
    # And they remain reachable in the served page for the mod to call.
    assert "function buildHelpEntries" in INDEX_HTML


def test_the_help_corpus_memo_is_dropped_on_a_first_login():
    # #173: /help-corpus.json serves the INSTALLED mods' help sections only to a
    # caller holding the token. A browser that arrives with no stored token and
    # opens Help before answering the login overlay therefore memoizes a corpus
    # with those sections missing -- and entering the token heals in place, it
    # does NOT reload, so without this they stay missing for the whole session.
    # Same shape as the mod-policy re-ask (test_mod_policy_applies_after_a_first
    # _login): the login success path notifies, the hook is HOME-broker-only.
    auth = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    assert "notifyHelpHostAuth(host.id)" in auth
    src = (BROKER_DIR / "80_js_help_window.js").read_text(encoding="utf-8")
    hook = _frag_fn(src, "function notifyHelpHostAuth(")
    assert "hostId !== lh.id" in hook          # the local broker only
    assert "helpCorpusGen++" in hook
    assert "helpCorpusPromise = null" in hook
    # A fetch already in flight when the memo is retired must neither write its
    # pre-token answer back into it NOR resolve its own caller with it (the help
    # mod re-reads the globals, but a mod calling this hoisted function need
    # not). Generation-stamped, and a superseded settle chains onto the current
    # generation instead of returning what it has.
    fetch = _frag_fn(src, "function fetchHelpCorpus(")
    assert "const gen = helpCorpusGen;" in fetch
    assert fetch.count("if (gen !== helpCorpusGen) return fetchHelpCorpus();") \
        == 2, "both the success and the failure path must chain"
    # ...and the chain cannot await itself, because the notify nulls the promise
    # in the same step it bumps the generation.
    assert hook.index("helpCorpusGen++") < hook.index("helpCorpusPromise = null")
    # The token is committed to the host BEFORE the notify, or the re-ask this
    # exists to trigger would go out unauthenticated and change nothing.
    assert auth.index("host.token = candidate") < auth.index(
        "notifyHelpHostAuth(host.id)")
    # An already-open Help window refreshes rather than waiting to be reopened:
    # 63's _onHostAuth loop runs AFTER the notify above, so the re-fetch there
    # sees a retired memo.
    help_mod = (BROKER_DIR / "mods" / "help"
                / "help.js").read_text(encoding="utf-8")
    assert "win._onHostAuth = (hid) => {" in help_mod
    assert auth.index("notifyHelpHostAuth(host.id)") < auth.index(
        "win._onHostAuth(host.id)")


def test_the_retired_help_corpus_is_invalidated_not_blanked():
    # The memo is retired by BUMPING THE GENERATION, never by nulling the
    # entries. buildHelpEntries reads `(helpCorpusEntries || [])` directly, and
    # this same login synchronously drives re-renders that go through it: 63
    # fires the async notifyModsHostAuth one line BEFORE notifyHelpHostAuth, and
    # its continuation (a /info + package round trip far cheaper than the
    # ~650 KB corpus) reaches _applyPolicyLive, so an installed mod whose init calls
    # ctx.registerHelpCards lands in _refreshHelpIfOpen -> refreshHelpCorpus ->
    # buildHelpEntries. With the entries nulled, that render snapshots an EMPTY
    # wiki corpus into the open Help window -- every wiki and shipped-mod section
    # gone until the refetch lands. Keep the stale corpus (it is exactly what the
    # window was already showing) and swap it whole on arrival.
    src = (BROKER_DIR / "80_js_help_window.js").read_text(encoding="utf-8")
    hook = _frag_fn(src, "function notifyHelpHostAuth(")
    for kept in ("helpCorpusEntries = null", "helpCorpusData = null"):
        assert kept not in hook, \
            f"{kept!r} blanks an open Help window mid-login; bump the gen instead"
    # What makes the bump sufficient: the early-return memo hit is
    # generation-scoped, so a login still forces exactly one refetch.
    fetch = _frag_fn(src, "function fetchHelpCorpus(")
    assert "helpCorpusEntries && helpCorpusEntriesGen === helpCorpusGen" in fetch
    assert "helpCorpusEntriesGen = gen;" in fetch
    # ...stamped in the same settle that installs the entries, or the next call
    # would either refetch forever or hand back a corpus from the wrong audience.
    body = fetch[fetch.index("helpCorpusEntries = flattenHelpCorpus(data);"):]
    assert body.index("helpCorpusEntriesGen = gen;") < body.index("return ")
    # A FAILED post-login refetch is the same story: return the stale corpus, not
    # [] -- the caller that renders the resolution must not blank the wiki either.
    assert "return helpCorpusEntries || [];" in fetch
    # And the renderer keeps tolerating a never-fetched memo (the first open,
    # before any corpus has landed at all).
    build = _frag_fn(src, "function buildHelpEntries(")
    assert "(helpCorpusEntries || [])" in build
    # The ordering that makes this reachable at all: 63 fires the async mods
    # re-ask BEFORE the (synchronous) help notify, so the mod-load continuation
    # is already racing the corpus refetch by the time the memo is retired.
    auth = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    assert auth.index("notifyModsHostAuth(host.id)") < auth.index(
        "notifyHelpHostAuth(host.id)")
    # #194: the help-card family moved verbatim into its own fragment (86 hit
    # the 2500-line cap); it is the same one <script>, so the call chain above
    # is unchanged.
    assert "_refreshHelpIfOpen();" in _frag_fn(
        _help_cards_src(), "function _modRegisterHelpCards(")


def test_help_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "help"
    js = mod_dir / "help.js"
    css = mod_dir / "help.css"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and css.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "help"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "help.js"
    # First mod to ship a packaged stylesheet via the S4 route (#77).
    assert meta["styles"] == ["help.css"]
    # The script registers the help mod, contributes the 'help' window kind through
    # ctx.registerWindowKind (#100, so its (+) launcher rides the mod's enable/
    # disable), and carries the moved window factory + chip. The redundant
    # showHelpButton toggle is gone (#101) — the chip follows the mod's enabled state.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'help'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'help'" in src
    assert "showHelpButton" not in src
    assert "function openHelpWindow" in src
    assert "function applyHelpButton" in src
    # XSS render-order invariant: helpAppendHighlighted must precede
    # findHelpWindow with no innerHTML between them (test_help_corpus.py's
    # test_help_render_path_has_no_innerhtml slices INDEX_HTML between the two).
    assert src.index("function helpAppendHighlighted(") \
        < src.index("function findHelpWindow(")
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the clock so the clock's "addStatusItem before #help-chip"
    # slot is preserved.
    assert "function openHelpWindow" in INDEX_HTML
    assert "id: 'help'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'clock'") < INDEX_HTML.index("id: 'help'")


def test_help_dev_docs_toggle_is_browser_local_and_survives_reset_local_view():
    # The Help window defaults to the END-USER guide; "Include developer docs"
    # is the opt-in that widens it. That is a per-BROWSER VIEW preference: two
    # people reading one broker must not flip each other's view, so it must
    # never ride the synced /state blob.
    src = (BROKER_DIR / "mods" / "help" / "help.js").read_text(encoding="utf-8")

    # (0) The control exists: a real checkbox with a visible label, built into
    #     the same .help-top row as the heading + search wrapper, and it ships
    #     in the served page.
    assert "devBox.type = 'checkbox'" in src
    assert "devLabel.textContent = 'Include developer docs';" in src
    assert "top.appendChild(devWrap);" in src
    for served in ("Include developer docs", "help-dev-toggle",
                   ".app-help .help-dev-toggle"):
        assert served in INDEX_HTML, f"missing from the served page: {served!r}"
    css = (BROKER_DIR / "mods" / "help" / "help.css").read_text(encoding="utf-8")
    assert ".app-help .help-dev-check" in css

    # (a) The pref key is UNDERSCORE-prefixed, which is load-bearing: "Reset
    #     local view" deletes every top-level pref whose key does NOT start with
    #     '_' (it reads them as per-session window geometry), so a bare 'help'
    #     key would be wiped by an unrelated recovery action.
    m = re.search(r"const HELP_PREFS_KEY = '([^']+)'", src)
    assert m, "help.js no longer declares HELP_PREFS_KEY"
    key = m.group(1)
    assert key.startswith("_"), \
        f"HELP_PREFS_KEY {key!r} would be wiped by resetLocalView"
    ident = (BROKER_DIR / "83_js_broker_identity.js").read_text(encoding="utf-8")
    reset = ident[ident.index("function resetLocalView()"):]
    assert "k.charAt(0) === '_'" in reset[:400], \
        "resetLocalView no longer spares underscore keys -- recheck HELP_PREFS_KEY"

    # (b) It also sits outside prefs._settings / prefs._layout, the only two
    #     things _stateBlob serializes, so the sync path cannot reach it.
    sync = (BROKER_DIR / "52_js_state_sync.js").read_text(encoding="utf-8")
    blob = sync[sync.index("function _stateBlob()"):sync.index("_stateSerialize")]
    assert key not in blob

    # (c) Persisted with savePrefsLocal(), never savePrefs() -- the latter would
    #     schedule a broker PUT of unrelated settings/layout on every tick. (The
    #     mod's savePrefs() calls elsewhere are for helpHintSeen, a deliberately
    #     SYNCED setting; the assertion is scoped to this path on purpose.)
    setter = src[src.index("function helpSetShowDevDocs"):]
    setter = setter[:setter.index("\n        function ")]
    assert "savePrefsLocal();" in setter and "savePrefs()" not in setter
    handler = src[src.index("const onDevToggle"):]
    handler = handler[:handler.index("devBox.addEventListener")]
    assert "savePrefs()" not in handler
    assert "helpSetShowDevDocs(devBox.checked);" in handler
    # Toggling repaints the OPEN window from the in-memory corpus (no refetch).
    assert "refreshHelpCorpus(win)" in handler

    # (d) Default UNTICKED, strictly: an absent OR corrupted stored value reads
    #     as false, so the end-user guide stays the default view.
    getter = src[src.index("function helpShowDevDocs"):]
    getter = getter[:getter.index("\n        function ")]
    assert "p.dev === true" in getter
    assert "devBox.checked = helpShowDevDocs();" in src

    # (e) The listener is torn down with the window, like every other one here.
    assert "devBox.removeEventListener('change', onDevToggle);" in src


def test_help_section_tier_reaches_the_client_and_gates_the_render():
    # #182: a wiki page declaring `<!-- help:tier dev -->` makes help_corpus.py
    # emit "tier": "dev" on its SECTION (a user-tier section carries no key at
    # all, which keeps the shipped sections byte-identical). Two halves have to
    # hold for the "Include developer docs" checkbox to mean anything: the tier
    # has to survive flattening into the flat per-card entries the renderer
    # groups, and the render path has to gate on it.
    core = (BROKER_DIR / "80_js_help_window.js").read_text(encoding="utf-8")
    src = (BROKER_DIR / "mods" / "help" / "help.js").read_text(encoding="utf-8")

    # (a) Carried across the flattening, alongside owner/secIcon -- and present
    #     in the served page, since the mod reads what core produced.
    flat = _frag_fn(core, "function flattenHelpCorpus(")
    assert re.search(r"\btier: sec\.tier\b", flat), \
        "flattenHelpCorpus drops sec.tier; the client can never see the tier"
    assert "tier: sec.tier" in INDEX_HTML

    # (b) The gate is an ALLOWLIST, and that is the non-negotiable shape.
    #     merge_installed_sections injects INSTALLED mods' help sections at
    #     serve time, bypassing the strict BuildError path that makes an unknown
    #     tier impossible for a shipped wiki page. A denylist ("hide only
    #     'dev'") would therefore DISPLAY anything malformed, misspelled or
    #     future-valued; the allowlist hides it. Hidden-by-default is the right
    #     failure direction for a control whose job is keeping content out of
    #     the default view.
    render = _frag_fn(src, "function renderHelpInto(")
    assert "const showDev = helpShowDevDocs();" in render
    assert "const tier = e.tier || 'user';" in render, \
        "an entry with no tier key must default to the user tier"
    assert "return tier === 'user' || (showDev && tier === 'dev');" in render, \
        "the tier gate must be an allowlist of exactly {user, dev}"
    # ...which is what makes an unknown tier hidden: the predicate accepts two
    # literals and nothing else, so there is no third value it can let through.
    assert set(re.findall(r"tier === '([a-z-]+)'", render)) == {"user", "dev"}
    # And no denylist crept back in beside it.
    for denylist in ("tier !== 'dev'", "!showDev &&", "e.tier === 'dev'"):
        assert denylist not in render, \
            "%r is a denylist; an unknown tier would be shown" % denylist

    # (c) It runs BEFORE the query filter. Gating afterwards would still keep
    #     developer cards off the rail at rest, but a query matching only
    #     developer prose would hand them straight back -- a filter that leaks
    #     on search is not a filter.
    assert "(e._hay || '').indexOf(q)" in render, "the query filter moved"
    assert render.index("tier === 'user'") < render.index("(e._hay || '')"), \
        "the tier gate must precede the query filter, not follow it"

    # (d) The rail counts, the section counts and the "N results" count all
    #     derive from the gated list, so a section whose cards are all hidden
    #     cannot survive as an empty category in the rail.
    assert "const n = entries.length;" in render
    assert "for (const e of entries) {" in render      # the group-by-slug pass
    assert "bySlug.get(slug).length" in render

    # (e) Toggling costs NO network request: refreshHelpCorpus (what the
    #     checkbox handler calls) re-snapshots from state already in memory and
    #     re-renders; neither it nor the render path fetches anything.
    refresh = _frag_fn(src, "function refreshHelpCorpus(")
    for net in ("hostFetch", "fetchHelpCorpus", "fetch("):
        assert net not in refresh, "toggling dev docs must not refetch (%s)" % net
        assert net not in render, "the render path must not fetch (%s)" % net

    # (f) The gate lives inside the XSS-boundary slice (helpAppendHighlighted ->
    #     findHelpWindow, guarded by test_help_render_path_has_no_innerhtml), so
    #     restate it locally: nothing added here may parse markup.
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML",
                      "DOMParser", "document.write"):
        assert forbidden not in render

    # (g) The transfer-size comments justify a network deadline, so a stale
    #     number there is the kind that gets believed. Rather than pin a
    #     literal that goes stale on the next wiki edit, MEASURE the corpus and
    #     require every "N KB" figure in the fragment to be within 15% of it.
    #     The figures were written for a ~200-300 KB corpus and the
    #     developer/operator pages roughly doubled it; this keeps them honest
    #     from here on without anyone having to remember.
    actual_kb = (BROKER_DIR / "help_corpus.json").stat().st_size / 1024
    quoted = [int(k) for k in re.findall(r"~(\d{3,4}) KB", core)]
    assert len(quoted) >= 2, "the corpus-size comments went missing from 80_js_help_window.js"
    for kb in quoted:
        assert abs(kb - actual_kb) / actual_kb < 0.15, (
            "80_js_help_window.js claims ~%d KB but the corpus is %.0f KB -- "
            "the comment justifies fetchHelpCorpus's deadline, so it has to be true"
            % (kb, actual_kb))

    # (h) And the whole gate ships in the served page.
    assert "return tier === 'user' || (showDev && tier === 'dev');" in INDEX_HTML


def test_register_help_cards_capability_present():
    # #78 (S5): ctx.registerHelpCards + the loader-side sanitizer (DOM-safe typed
    # block/span schema, never raw HTML) + the window.__mods.helpCards registry
    # ride in the served loader. The Playwright acceptance (a fixture mod's cards
    # appear in Help) depends on these.
    for sym in ("registerHelpCards: function", "function _modRegisterHelpCards",
                "function _sanitizeHelpCard", "function _sanitizeHelpBlocks",
                "helpCards:"):
        assert sym in INDEX_HTML, f"missing registerHelpCards symbol: {sym!r}"


# --------------------------------------------------------------------------- #
# window-kind registry (#80 / S7)
# --------------------------------------------------------------------------- #

def test_window_kind_registry_core_present():
    # The registry primitives ride in the served page: the no-TDZ getter, the
    # register/lookup/list/delete helpers, the shared serializer, and the lazy
    # built-in population. The Playwright acceptance drives these via globals +
    # window.__mods.__test.windowKinds.
    for sym in ("function _windowKindRegistry", "function registerWindowKind",
                "function deleteWindowKind", "function lookupWindowKind",
                "function windowKindMenuList", "function registerBuiltinWindowKinds",
                "function serializeAppWindow", "function openNoteOrEditorWindow",
                "windowKinds: function"):
        assert sym in INDEX_HTML, f"missing window-kind registry symbol: {sym!r}"


def test_register_window_kind_capability_present():
    # #80 (S7): ctx.registerWindowKind + its loader-side wrapper (validate via the
    # core registerWindowKind, teardown that removes exactly this registration).
    # The fixture-mod acceptance (a brand-new kind end-to-end) depends on these.
    for sym in ("registerWindowKind: function", "function _modRegisterWindowKind",
                "deleteWindowKind(entry.appKind, entry)"):
        assert sym in INDEX_HTML, f"missing registerWindowKind symbol: {sym!r}"


def test_window_kind_builtins_registered_in_menu_order():
    # registerBuiltinWindowKinds registers the ONE remaining core kind (control-
    # panel); sticky-note left for the S8 mod #81, text-editor for the S10 mod #83,
    # file-manager for the S11 mod #84, task-manager for the S12 mod #85, and help
    # for the #100 mod. Registration order is the historical (+) launch-menu order
    # (Map iteration order drives the menu).
    src = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    order = ["control-panel"]
    positions = []
    for kind in order:
        needle = f"appKind: '{kind}'"
        assert needle in src, f"built-in kind not registered: {kind}"
        positions.append(src.index(needle))
    assert positions == sorted(positions), \
        "built-in kinds must register in the historical menu order"
    # sticky-note, text-editor, file-manager, task-manager + help are now mods, never
    # core built-ins (each appends through ctx at loadMods time). The sticky note's
    # retain-on-close rode with it; the editor / file-manager / task-manager / help
    # specs rode with them too (#100 moved Help's registration into mods/help/).
    assert "appKind: 'sticky-note'" not in src
    assert "appKind: 'text-editor'" not in src
    assert "appKind: 'file-manager'" not in src
    assert "appKind: 'task-manager'" not in src
    assert "appKind: 'help'" not in src
    assert "retainOnClose: function (rec)" not in src
    # No persisted CORE built-in remains: the file-manager's serializeAppWindow
    # reference moved to mods/file-manager/ (text-editor's to mods/editor/,
    # sticky's to mods/sticky/), so core registers ZERO `serialize:` built-ins
    # (the sole survivor control-panel is ephemeral).
    assert src.count("serialize: serializeAppWindow") == 0


def test_sticky_symbols_removed_from_core_fragments():
    # The sticky note is now a mod (#81/S8): its registry registration is gone from
    # core 54, and its launcher + Closed-notes builder are gone from core 76 (both
    # moved verbatim into mods/sticky/sticky.js). The shared builder
    # (openNoteOrEditorWindow) moved to mods/editor/ (#83/S10, the text editor owns
    # it now); the serializer (serializeAppWindow) deliberately STAYS in core (the
    # file-manager built-in + both mods share it). The sticky mod calls back into
    # openNoteOrEditorWindow + serializeAppWindow — both reachable in the served
    # page regardless of which fragment/mod ships them.
    core = {
        "54_js_app_windows_store.js": ("appKind: 'sticky-note'",
                                       "retainOnClose: function (rec)"),
        "76_js_launch_fullscreen.js": ("function launchStickyNote",
                                       "function closedAppMenuItems"),
    }
    for name, symbols in core.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The helpers the sticky mod calls back into are still present + reachable in
    # the served page (openNoteOrEditorWindow now ships in mods/editor/).
    for sym in ("function openNoteOrEditorWindow", "function serializeAppWindow"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_sticky_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "sticky"
    js = mod_dir / "sticky.js"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "sticky"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "sticky.js"
    # The script registers the sticky mod and contributes the sticky-note window
    # kind through ctx.registerWindowKind, reusing the core serializer + builder so
    # persistence (webterm:appwindows:v1) stays byte-identical.
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'sticky'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'sticky-note'" in src
    assert "serialize: serializeAppWindow" in src
    # #141: the factory delegates to the shared builder through a wrapper that
    # adds a taskbar chip when the stickyTaskbar toggle is on.
    assert "openNoteOrEditorWindow(d)" in src
    assert "ctx.settings.boolean(" in src
    assert "'stickyTaskbar'" in src
    assert "tiers: ['settings', 'window']" in src
    # The retain trim + Closed-notes menu rode along with the kind.
    assert "retainOnClose: function (rec)" in src
    assert "Closed notes" in src
    # The toggle ships in the served page too.
    assert "'stickyTaskbar'" in INDEX_HTML
    # And the mod ships in the served page (present in the mod / gone from core),
    # registered AFTER the help mod (its position in _MODS).
    assert "id: 'sticky'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'sticky'")


# --------------------------------------------------------------------------- #
# text-editor mod (#83 / S10)
# --------------------------------------------------------------------------- #

def test_editor_symbols_removed_from_core_fragments():
    # The text editor is now a mod (#83/S10): its built-in registration is gone
    # from core 54, its launcher from core 76, and the AGENTS.md hooks
    # (openAgentDocsWindow + openAgentsMdEditor) from core 73. The editor kind +
    # builder + launcher live in mods/editor/; as of #120 the two AGENTS.md hooks
    # were split further into their own mods/agent-docs/ mod (requires the
    # editor), which #177 then retired to mods-deprecated/ — so they are gone
    # from core AND from the served page now.
    # The CodeMirror fragment (69) + editor fragment (70) are DELETED;
    # openAppWindow (the dispatcher) moved into core 54.
    assert not (BROKER_DIR / "69_js_codemirror.js").exists()
    assert not (BROKER_DIR / "70_js_editor_app.js").exists()
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'text-editor'",
                                       "function openNoteOrEditorWindow"),
        "73_js_window_runtime.js": ("function openAgentDocsWindow",
                                    "function openAgentsMdEditor"),
        "76_js_launch_fullscreen.js": ("function launchTextEditor",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # openAppWindow (the central dispatcher) moved into core 54, NOT the mod.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "function openAppWindow" in s54
    # And the moved builder/hooks are present + reachable in the served page as
    # hoisted functions, so core reaches them mods-off. The builder/loader/
    # launcher ship in mods/editor/, concatenated into the one shared <script>.
    for sym in ("function openNoteOrEditorWindow", "function loadCodeMirror",
                "function launchTextEditor"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"
    # #120: the two AGENTS.md openers moved OUT of mods/editor/ and INTO
    # mods/agent-docs/ — which #177 then retired to mods-deprecated/. They live
    # in the retired copy, not in editor.js, and reach the served page from
    # NEITHER (see test_agent_docs_mod_retired_to_deprecated_tree).
    editor_src = (BROKER_DIR / "mods" / "editor" / "editor.js").read_text(encoding="utf-8")
    agent_src = (BROKER_DIR / "mods-deprecated" / "agent-docs"
                 / "agent-docs.js").read_text(encoding="utf-8")
    for sym in ("function openAgentDocsWindow", "function openAgentsMdEditor"):
        assert sym in agent_src, f"{sym!r} must live in the agent-docs mod (#120)"
        assert sym not in editor_src, f"{sym!r} must be gone from editor.js (#120)"


def test_editor_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "editor"
    editor_js = mod_dir / "editor.js"
    cm_js = mod_dir / "codemirror.js"
    manifest = mod_dir / "mod.json"
    assert editor_js.is_file() and cm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "editor"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "editor.js"
    # Both mod scripts are declared in _MODS (the codemirror lazy loader + the
    # editor), so the .js drift guard accepts them.
    assert "mods/editor/codemirror.js" in ui._MODS
    assert "mods/editor/editor.js" in ui._MODS
    src = editor_js.read_text(encoding="utf-8")
    # Registers the editor mod + contributes the text-editor window kind through
    # ctx.registerWindowKind, reusing the shared core serializer + builder.
    assert "registerMod(" in src
    assert "id: 'editor'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'text-editor'" in src
    assert "serialize: serializeAppWindow" in src
    assert "return openNoteOrEditorWindow(d)" in src
    assert "return launchTextEditor()" in src
    # File I/O rides ctx.file (#82): the mod stashes ctx.file and every /file/*
    # call flows through editorFile() — NO direct fileApiPost survives in the mod.
    assert "editorFile.cap = ctx.file;" in src
    assert "editorFile().read(" in src
    assert "editorFile().write(" in src
    assert "editorFile().list(" in src
    assert "fileApiPost(" not in src, "editor mod must route I/O through ctx.file"
    # The CodeMirror loader rode along as a separate file (helpers only, no
    # registerMod), so it can stay a small fragment.
    cm = cm_js.read_text(encoding="utf-8")
    assert "function loadCodeMirror" in cm and "function detectLanguage" in cm
    assert "registerMod(" not in cm
    # Ships in the served page, AFTER the help mod, BEFORE the sticky mod (so the
    # (+) menu lists Text editor before Sticky note, after the core built-ins).
    assert "id: 'editor'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'editor'")
    assert INDEX_HTML.index("id: 'editor'") < INDEX_HTML.index("id: 'sticky'")


def test_agent_docs_mod_retired_to_deprecated_tree():
    # #177: the Agent-docs mod (#120 — the tabbed AGENTS.md/CLAUDE.md editor
    # opened from each terminal's 📋 button) is RETIRED. Its 📋 opened whatever
    # the session's INFERRED cwd happened to be, so a wrong inference silently
    # opened — and, on save, wrote to — a different project's AGENTS.md. The
    # inference is core's (psutil over the process tree), not this mod's, so the
    # mod is retired recoverably rather than deleted: it moves verbatim into
    # mods-deprecated/, which nothing loads.
    #
    # This test is the anti-rot guard the retirement is worth only if it exists:
    # the retired copy must stay COMPLETE (so the README's copy-back steps work
    # on a clean checkout) and must stay UNSHIPPED.
    import json
    mod_dir = BROKER_DIR / "mods-deprecated" / "agent-docs"
    js = mod_dir / "agent-docs.js"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and manifest.is_file() and help_md.is_file(), \
        "the retired agent-docs copy must stay whole — it is the re-enable path"
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "agent-docs"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "agent-docs.js"
    assert meta["help"]["slug"] == "agent-docs"
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'agent-docs'" in src
    # The hard dependency on the editor mod (#121) — copying it back re-adds a
    # mod that MUST be listed after mods/editor/editor.js in _MODS.
    assert "requires: ['editor']" in src
    assert "function openAgentDocsWindow" in src
    assert "function openAgentsMdEditor" in src
    # The parent README is the mechanism, not a courtesy: it is the only place
    # the copy-back steps (and why `mods_dir` is NOT one of them) are written.
    readme = (BROKER_DIR / "mods-deprecated" / "README.md")
    assert readme.is_file()
    readme_txt = readme.read_text(encoding="utf-8")
    assert "agent-docs" in readme_txt and "_MODS" in readme_txt

    # UNSHIPPED. _MODS is the allowlist that ships a mod, so its absence there is
    # the retirement; the served page must carry no trace of the mod.
    assert not any("agent-docs" in rel for rel in ui._MODS)
    assert not (BROKER_DIR / "mods" / "agent-docs").exists(), \
        "the retired mod must not also sit in mods/ (the _MODS drift guard " \
        "rglobs mods/**/*.js and would fail too)"
    for sym in ("id: 'agent-docs'", "function openAgentDocsWindow",
                "function openAgentsMdEditor", "btn-agentsmd"):
        assert sym not in INDEX_HTML, f"{sym!r} must be gone from the served page (#177)"
    assert "agent-docs" not in {m["id"] for m in ui.mod_catalog()}

    # ...and the editor's legacy-record upgrade branch, which called the retired
    # opener as a hoisted free identifier, is GUARDED. `typeof` is the only test
    # that is safe on an undeclared identifier — a bare call would ReferenceError
    # for any stored record with `agentsMdCwd` and no `docs`.
    editor_src = (BROKER_DIR / "mods" / "editor"
                  / "editor.js").read_text(encoding="utf-8")
    assert "typeof openAgentDocsWindow === 'function'" in editor_src
    assert "openAgentDocsWindow(" in editor_src   # the guarded call still there
    # The tabbed docs/Sections machinery STAYS in editor.js on purpose, so an
    # already-stored Agent-docs window still restores and works. Only the two
    # entry points (and the 📋 button) went away.
    assert "appData.docs" in editor_src
    assert "kind: 'sections'" in editor_src


def test_retired_agent_docs_would_still_load_if_copied_back():
    # The README promises the retired copy re-enables on a clean checkout, and a
    # manifest-shape assertion cannot back that: a retired mod rots by having the
    # HOST rename something out from under it, not by editing itself. So hold the
    # two contracts a copy-back actually depends on.
    #
    # (1) Rule 1 of the portable-mod lint, which every SHIPPED mod passes and
    # _shipped_mod_scripts() no longer scans for this one: nothing may RUN at
    # top level but registerMod(...). Spliced back into the inline bundle,
    # top-level code would run at parse time.
    src_path = BROKER_DIR / "mods-deprecated" / "agent-docs" / "agent-docs.js"
    bad = [s for s in _js_top_level_statements(src_path.read_text(encoding="utf-8"))
           if s != "registerMod" and not _JS_DECL.match(s)]
    assert not bad, f"retired agent-docs runs code at top level: {bad[:4]}"

    # (2) Every core/editor name it calls as a hoisted FREE IDENTIFIER still
    # exists in the served page. This is the real rot: rename or drop any of
    # these and the retired copy is broken long before anyone tries to copy it
    # back, with nothing to say so. `editorFile` in particular is the editor
    # mod's ctx.file choke point and lost its only external caller in #177 --
    # exactly the kind of now-unused seam a later cleanup deletes.
    src = src_path.read_text(encoding="utf-8")
    for name in ("editorFile", "openAppWindow", "showNotice", "hostById",
                 "localHost", "joinNative", "revealAndFocusWindow",
                 "findKeyInLayout", "visibleColIndex", "placeWindowTiled",
                 "tabWindowIntoTile", "registerMod"):
        assert name + "(" in src, \
            f"{name!r} is no longer called by the retired mod — drop it here"
        assert "function " + name in INDEX_HTML, (
            f"the retired agent-docs mod calls {name}() as a free identifier, "
            "but the served page no longer declares it. Either restore the name "
            "or update mods-deprecated/agent-docs/ (and its README note).")
    # ...and the shared maps it reads, which are core `const`s, not functions.
    for name in ("sessions", "windows"):
        assert "const " + name in INDEX_HTML


def test_scratchpad_mod_packaged_and_manifest_agrees():
    # #124: the scratchpad — a singleton, server-backed (ctx.serverStore) notes
    # window with internal CodeMirror tabs + a revision-history panel. It REQUIRES
    # the editor mod (shares its single CM build via loadCodeMirror) and adds its
    # own 'scratchpad' window kind. Content is server-only; the localStorage record
    # carries view state only.
    import json
    mod_dir = BROKER_DIR / "mods" / "scratchpad"
    js = mod_dir / "scratchpad.js"
    css = mod_dir / "scratchpad.css"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and css.is_file() and manifest.is_file() \
        and help_md.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "scratchpad"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "scratchpad.js"
    assert meta["styles"] == ["scratchpad.css"]
    assert meta["help"]["slug"] == "scratchpad"
    # Declared in _MODS, AFTER the editor it depends on (also enforced by the #121
    # static ordering guard, test_requires_declared_before_dependency_...).
    assert "mods/scratchpad/scratchpad.js" in ui._MODS
    assert ui._MODS.index("mods/editor/editor.js") \
        < ui._MODS.index("mods/scratchpad/scratchpad.js")
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'scratchpad'" in src
    assert "ctxVersion: 1" in src
    # The hard dependency on the editor mod (the #121 requires primitive).
    assert "requires: ['editor']" in src
    # Registers its own window kind; content rides ctx.serverStore, NOT the file
    # API (a same-origin scratchpad has no business doing host /file/* I/O).
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'scratchpad'" in src
    assert "ctx.serverStore" in src
    assert "fileApiPost(" not in src
    # Builds its CM editors on the editor's ONE shared build (never a 2nd import).
    assert "loadCodeMirror()" in src
    # serialize persists view state only — never note content (that lives on the
    # server). The record must not carry a tabs/text/content field.
    assert "appKind: 'scratchpad', open: true" in src
    assert "text:" not in src.split("serialize: function")[1].split("}")[0]
    # Ships in the served page, AFTER the editor mod (the CM-build dependency) and
    # AFTER the clipboard mod (the last mod before it in _MODS).
    assert "id: 'scratchpad'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'editor'") < INDEX_HTML.index("id: 'scratchpad'")
    assert INDEX_HTML.index("id: 'clipboard'") \
        < INDEX_HTML.index("id: 'scratchpad'")


def test_host_registry_mod_packaged_and_manifest_agrees():
    # #65: the host-registry mod — an optional shared broker list published to /
    # pulled from a broker via ctx.serverStore, with a browser-mounted settings
    # pane. The host list stays browser-local (prefs._hosts); the mod only reads/
    # writes a server-side COPY. First consumer of registerSettingsPane, and of
    # the mount:'browser' + serverStore opts.host / purgeRevisions extensions.
    import json
    mod_dir = BROKER_DIR / "mods" / "host-registry"
    js = mod_dir / "host-registry.js"
    css = mod_dir / "host-registry.css"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and css.is_file() and manifest.is_file() \
        and help_md.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "host-registry"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "host-registry.js"
    assert meta["styles"] == ["host-registry.css"]
    assert meta["help"]["slug"] == "host-registry"
    # help order sits ABOVE the recorder's 2090 (2090 is taken) so mod help
    # cards keep a stable, collision-free order.
    assert meta["help"]["order"] > 2090
    # Declared LAST in _MODS (no mod depends on it; nothing depends on it).
    assert "mods/host-registry/host-registry.js" in ui._MODS
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'host-registry'" in src
    assert "ctxVersion: 1" in src
    # Tiers match the order-sensitive _EXPECTED_TIERS guard above.
    assert "tiers: ['storage', 'settings']" in src
    # Built on the durable server store, feature-detected, and mounts a pane in
    # the Browser settings tab — never doing host /file/* I/O.
    assert "if (!ctx.serverStore) return;" in src
    assert "ctx.registerSettingsPane(" in src
    assert "mount: 'browser'" in src
    assert "fileApiPost(" not in src
    # The core host model is NOT relocated: the mod is a layer over the existing
    # browser-local prefs._hosts, so it reaches the shared closure directly.
    assert "getHosts()" in src
    assert "savePrefs()" in src
    # Publishing tokens is opt-in + revocable: the purge flag rides set()'s opts.
    assert "purgeRevisions: true" in src
    # All untrusted text is textContent, never innerHTML (labels/urls/tokens).
    assert ".innerHTML" not in src
    _assert_host_registry_encryption(src)
    _assert_host_registry_pull_sources(src)
    _assert_host_registry_no_history(src)


def _assert_host_registry_no_history(src):
    # #192: the modstore revision ring used to archive every token this mod
    # published. The migration is (a) the sticky noHistory flag on EVERY publish
    # path, (b) a PRE-WRITE per-host /info capability gate with a named
    # override, and (c) a per-host "still archiving" backstop. These are the
    # structural facts; the server half is tested in
    # tests/test_modstore_nohistory.py.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    # (a) Every publish path flags the record. publishTo builds ONE opts object
    # and both set() calls share it, so the 409 rebase retry cannot re-PUT
    # without the flag (a retry that drops it silently resumes archiving).
    assert "opts.noHistory = true;" in code
    assert code.count("ctx.serverStore.set(value, rev, opts)") == 1
    assert code.count("ctx.serverStore.set(value, res.rev, opts)") == 1
    assert "purgeRevisions: true } : { host: hid }" in code
    # Forget keeps its per-write purge (the half an old broker still honours)
    # AND flags the record so the NEXT write cannot start a fresh ring.
    assert "{ purgeRevisions: true, noHistory: true }" in code
    # (b) The gate reads /info BEFORE any password-bearing PUT: the flag fails
    # open on an old broker, so the discovering PUT would itself be the leak.
    # ABSENCE of the key is the old-build signal, never an error (#157).
    assert "function infoAdvertisesNoHistory(" in code
    assert "info.modstore" in code and "m.noHistory === true" in code
    assert "hostFetch(host, '/info'" in code
    assert "function checkNoHistory(" in code
    # …resolved per host, and never through hostFetch(null, …), which would
    # silently probe THIS page's own broker instead (#174).
    assert "(hid === 'local') ? localHost() : hostById(hid)" in code
    assert "why: 'host not found'" in code
    # Gated on a password-bearing publish only, and the refused hosts are
    # dropped from the write list — the others still go out.
    assert "const carried = valueHasPlainTokens(value);" in code
    assert "let writeTo = targets;" in code
    assert "for (const hid of writeTo)" in code
    assert "publishTo(hid, out, !!plan.seal," in code, (
        "the publish loop must still hand publishTo the seal flag -- and, "
        "since the verify round, whether capability was already proven")
    # The override is NAMED, per host, and never silent: a dialog with the
    # archiving consequence spelled out, defaulting to skip.
    assert "function pickArchivingOverride(" in code
    assert "files it into a revision history on " in src
    assert "Publish passwords anyway" in src
    assert "Skip these brokers" in src
    assert "if (!res || res.value !== 'anyway') return out;" in code
    # (c) The response-echo backstop, PER HOST — a broker that ignored the flag
    # does not echo it, and that host says so on its own row rather than being
    # averaged into the success count.
    assert "archiving: ok && res.noHistory !== true" in code
    assert "results.filter(r => r.ok && r.archiving)" in code
    assert "function archivingList(" in code
    assert "still archiving." in src
    assert "Still archiving" in src


def _assert_host_registry_pull_sources(src):
    # #174: Pull reads ANY configured broker, not just the one whose page is
    # open. The BEHAVIOUR is tested by executing the shipped code
    # (tests/test_host_registry_sources.py); these are the structural facts and
    # the negatives that a round-trip test cannot see.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    # It rides the routing publish has always had. No new endpoint, and no
    # second transport.
    assert "ctx.serverStore.get({ host: host.id })" in code
    assert "function probeSource(" in code
    # Reading somebody else's broker means the failures must be
    # distinguishable: the old "any non-200 means no registry yet" is right for
    # the local broker only.
    assert "function sourceTransportState(" in code
    assert "refused our password" in src
    # A source with no saved password is NOT probed: it could only 401, and the
    # request would tell that broker we tried for nothing.
    assert "if (!local && !(host.token))" in code
    # fetch() resolves on HEADERS, so hostFetch's deadline does not cover a
    # body that never finishes arriving. Each probe carries its own.
    assert "function withDeadline(" in code
    assert "PROBE_MS" in code
    # No forced auth prompt, ever — reading N brokers must not pop N prompts.
    assert "promptFileHostAuth" not in code
    assert "promptHostAuth" not in code
    # Provenance is stamped from the SOURCE LIST onto a wrapper, never read off
    # the value: a row that lies about which machine told us is exactly the
    # wrong thing to show. classify() takes mergeSources' items, so there is no
    # path that reaches a row without going through the merge.
    assert "function mergeSources(" in code
    assert "function classify(items)" in code
    assert "row.src = it.src;" in code
    assert "classify(merged.items)" in code
    assert "classify(mergeSources(" in code          # the single-source shorthand
    # broker_id is NOT a merge key (it is unverified input, so keying on it
    # would let one record swallow another's identity) — a second address
    # claiming one identity is a CONFLICT that keeps both rows.
    assert "byBroker" in code and "item.conflict" in code
    # …and it is no longer written into prefs on an apply, nor is the incoming
    # `id` a match candidate at all.
    assert "local.brokerId = '';" in code
    assert "h.id === e.id" not in code
    # A remote list may not hand us a loopback host, an invisible host, or a
    # password we did not ask for.
    assert "droppedLoopback" in code
    assert "e.hidden = false;" in code
    assert "acceptRemoteTokens" in code
    # The load-time discovery notice stays LOCAL-only and still never prompts.
    notice = src[src.index("// ---- one-time discovery notice"):]
    assert "serverStore.get()" in notice
    assert "{ host:" not in notice


def _assert_host_registry_encryption(src):
    # #175: client-side encryption of the published value. The BEHAVIOUR is
    # tested by executing the shipped code in node
    # (tests/test_host_registry_crypto.py); these are the structural facts that
    # file cannot see, plus the negatives -- a thing being ABSENT is exactly
    # what a round-trip test can't prove.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    # The node harness slices between these two markers and needs the range to
    # stay declaration-only. If either disappears those tests stop running.
    assert "// ---- pure model + crypto ---" in src
    assert "// ---- dialogs ---" in src
    assert src.index("// ---- pure model + crypto ---") \
        < src.index("// ---- dialogs ---")
    # WebCrypto only -- no vendored/hand-rolled cipher, no third-party import.
    assert "window.crypto.subtle" in code
    assert "'PBKDF2'" in code and "'SHA-256'" in code
    assert "AES-GCM" in code
    # …and gated on the secure context, like the clipboard APIs (#153), because
    # the broker terminates no TLS: on http://<lan-ip>:4445 crypto.subtle is
    # simply undefined.
    assert "window.isSecureContext" in code
    # The mode is a SYNCED setting with all three options ALWAYS registered.
    # Dropping the unusable ones would make an intent this page can't act on
    # read as 'off', and encPlan could no longer tell "the user said publish in
    # the clear" from "this page can't encrypt" -- which is the difference
    # between an intended plaintext publish and a silent downgrade.
    assert "ctx.settings.select('hostRegEncrypt'" in src
    assert "def: 'tokens'" in src
    for mode in ("'off'", "'tokens'", "'all'"):
        assert f"value: {mode}" in src
    # Fail closed: encPlan is the ONE place that decides, and `blocked` is a
    # real outcome rather than a fallback to publishing in the clear.
    assert "function encPlan(" in src
    assert "blocked: true" in src
    assert "if (plan.blocked)" in src
    # The passphrase is never persisted: no ctx.storage / localStorage write of
    # it, and no "remember me" anywhere.
    # The ONE thing it stores is the discovery-notice nonce: written by the
    # notice's two branches, and by a successful publish so the browser that
    # published is never nudged about its own list.
    assert code.count("ctx.storage.set(NOTIFIED_KEY") == 3
    assert code.count("ctx.storage.set(") == 3
    for forbidden in ("localStorage", "sessionStorage", "set(_encPass",
                      ", _encPass)", "JSON.stringify(_encPass"):
        assert forbidden not in code, \
            f"the passphrase must never be persisted: {forbidden!r}"
    assert "ctx.onUnload(function () { _encPass = null; });" in src
    # A real password field, not openDialog's `fields` (those are type=text).
    assert "i.type = 'password';" in code
    # The encrypted publish clears the revision ring: without it the plaintext
    # value this write replaces stays in the history of the store we just
    # stopped trusting, and "the broker only stores ciphertext" is false.
    assert "publishTo(hid, out, !!plan.seal," in code, (
        "the publish loop must still hand publishTo the seal flag -- and, "
        "since the verify round, whether capability was already proven")
    assert "purgeRevisions: true } : { host: hid }" in code
    # The load-time discovery notice must never prompt: it runs with no user
    # interaction, and a page-load password prompt is a habit worth not
    # teaching. The whole-list branch reports and stops.
    notice = src[src.index("// ---- one-time discovery notice"):]
    for forbidden in ("openPassphraseDialog", "encOpen(", "requirePassphrase"):
        assert forbidden not in notice, \
            f"the discovery notice must not unlock anything: {forbidden!r}"
    # Nothing displayed is sourced from the stored value: `encNote` is written
    # for whoever opens the JSON by hand and is never read back (a
    # writer-controlled human string is a phishing surface).
    assert "encNote: ENC_NOTE" in code
    assert "value.encNote" not in code and ".encNote)" not in code
    # Ships in the served page, AFTER the recorder mod (the last mod before it).
    assert "id: 'host-registry'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'recorder'") \
        < INDEX_HTML.index("id: 'host-registry'")
    # Its CSS rides the served page via the mod-css splice.
    assert ".hostreg-list" in INDEX_HTML
    # The Browser-pane mount anchor exists and sits inside #set-pane-browser
    # (so a mounted section is hidden on every non-Browser tab by the pane
    # itself, never leaking onto a remote-host tab — #65 containment).
    body = (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    assert 'id="set-browser-mods"' in body
    browser_pane = body.index('id="set-pane-browser"')
    anchor = body.index('id="set-browser-mods"')
    troubleshoot = body.index("Troubleshooting")
    assert browser_pane < anchor < troubleshoot


def test_mousemode_mod_packaged_and_manifest_agrees():
    # #155: the ambient 🖱 title-bar chip, shown for exactly as long as a
    # full-screen app owns the mouse. A pure READER: it rides
    # ctx.windows.onTerminalCreate and samples xterm's own public modes getter,
    # so it needs no other ctx capability and touches no broker endpoint.
    import json
    mod_dir = BROKER_DIR / "mods" / "mousemode"
    js = mod_dir / "mousemode.js"
    css = mod_dir / "mousemode.css"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and css.is_file() and manifest.is_file() \
        and help_md.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "mousemode"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "mousemode.js"
    assert meta["styles"] == ["mousemode.css"]
    assert meta["help"]["slug"] == "mousemode"
    assert "mods/mousemode/mousemode.js" in ui._MODS
    src = js.read_text(encoding="utf-8")
    # The negative assertions below are about CODE, not prose — the mod's header
    # comment discusses the alternatives it rejected by name.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    assert "registerMod(" in src
    assert "id: 'mousemode'" in src
    assert "ctxVersion: 1" in src
    # Tiers match the order-sensitive _EXPECTED_TIERS guard above.
    assert "tiers: ['window']" in src
    # Ships default-ON (no defaultEnabled:false): the chip is invisible until an
    # app grabs the mouse, so it costs a shell-only user nothing. Asserted so
    # flipping it off becomes a deliberate, reviewed change.
    assert "defaultEnabled" not in src
    # The per-terminal hook is feature-detected, like git/termfont.
    assert "if (!ctx.windows) return;" in src
    # The chip's ONE source of truth is xterm's public modes getter, sampled on
    # the public onWriteParsed event — not the internal enable-mouse-events class
    # xterm toggles, and not a per-window timer (#155 rules that out).
    assert "mouseTrackingMode" in src
    assert "onWriteParsed" in src
    assert "setInterval" not in src
    # The modes getter ALLOCATES on every read and a flooding terminal parses
    # far more writes than it paints, so onWriteParsed only arms one rAF — and
    # the queued frame is cancelled on teardown, never left to fire onto a
    # removed node. This is what makes default-ON defensible.
    assert "requestAnimationFrame(" in src
    assert "cancelAnimationFrame(" in src
    # No platform sniffing: both gestures are named unconditionally, because
    # navigator.platform is deprecated/spoofable and misreads iPadOS.
    for gone in ("navigator.platform", "userAgentData", "navigator.userAgent"):
        assert gone not in code, f"mousemode must not sniff the platform: {gone!r}"
    # The tooltip states the MEASURED state. "Mouse reporting is on" is true
    # whenever the mode is set; "this app is reading the mouse" is not — a
    # killed TUI or a cat'd escape sequence leaves reporting on with nobody
    # listening.
    assert "Mouse reporting is on" in src
    # Read-only and side-effect-free on the terminal: it must never write to the
    # PTY, wrap term.write, or touch mouse reporting itself.
    for forbidden in ("term.write", "sendChunked", "term.paste", "ctx.session",
                      "ctx.file", "hostFetch"):
        assert forbidden not in code, f"mousemode must stay a reader: {forbidden!r}"
    # Teardown covers BOTH exits (window close + mod disable), the git idiom.
    assert "info.onDispose(teardown)" in src
    assert "ctx.onUnload(" in src
    assert "disp.dispose()" in src
    # Tooltip text is platform-correct: Shift-drag forces a selection everywhere
    # EXCEPT macOS, where #154's macOptionClickForcesSelection makes it
    # Option-drag. Both spellings must be present, chosen at init.
    assert "Shift-drag" in src and "Option-drag" in src
    # Ships in the served page, AFTER the host-registry mod (the last mod before
    # it in _MODS), and its CSS rides the mod-css splice.
    assert "id: 'mousemode'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'host-registry'") \
        < INDEX_HTML.index("id: 'mousemode'")
    assert ".mousemode-chip" in INDEX_HTML


def test_mod_sync_mod_packaged_and_manifest_agrees():
    # #158: pushes this broker's mod setup (which mods are on + the settings
    # mods own) to selected peers, and adopts a peer's into this browser. The
    # whole feature rides wires that already exist -- GET /info for a peer's
    # catalog + pins (#157), POST /mods/policy to write them, GET/PUT /state for
    # the settings blob -- so it adds NO broker endpoint.
    import json
    mod_dir = BROKER_DIR / "mods" / "mod-sync"
    js = mod_dir / "mod-sync.js"
    css = mod_dir / "mod-sync.css"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and css.is_file() and manifest.is_file() \
        and help_md.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "mod-sync"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "mod-sync.js"
    assert meta["styles"] == ["mod-sync.css"]
    assert meta["help"]["slug"] == "mod-sync"
    assert "mods/mod-sync/mod-sync.js" in ui._MODS
    # The mod id is also a POLICY KEY namespace, so it must satisfy the shape the
    # broker enforces on /mods/policy keys (_MODSTORE_ID_RE) -- a pin naming a mod
    # the broker would reject is unwritable.
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", meta["id"])
    src = js.read_text(encoding="utf-8")
    # The negative assertions below are about CODE, not prose -- the mod's header
    # comment names the alternatives it rejected.
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    assert "registerMod(" in src
    assert "id: 'mod-sync'" in src
    assert "ctxVersion: 1" in src
    # Tiers match the order-sensitive _EXPECTED_TIERS guard above.
    assert "tiers: ['settings']" in src
    # Ships default-ON (no defaultEnabled:false): it is inert until a button is
    # clicked, so it costs a single-broker user nothing.
    assert "defaultEnabled" not in src
    # It must NEVER disable itself: adopting a snapshot that turned mod-sync off
    # would run this mod's own teardown mid-apply, with its dialog still open,
    # orphaning the rest of the batch. Self is excluded in BOTH directions.
    assert "const SELF = 'mod-sync';" in src
    assert "if (m.id === SELF) continue;" in code
    # --- the two anti-clobber properties #158 turns on ---
    # 1. It must NOT reuse putHostState for the settings half: that helper's 409
    #    rebase adopts the winner's rev+layout but re-PUTs the WHOLE settings
    #    object it was handed, which would erase every key a concurrent editor
    #    changed -- the blind overwrite the issue forbids. Its own writer
    #    re-reads the live blob and re-applies only ITS keys, so the merge is
    #    per-key. (fetchHostState is avoided for the second half of the same
    #    reason: it normalizes a peer's blob with THIS build's rules in place.)
    for forbidden in ("putHostState", "fetchHostState"):
        assert forbidden not in code, \
            f"mod-sync must own its /state merge, not reuse {forbidden!r}"
    assert "async function writeSettings" in src
    assert "clientId: CLIENT_ID" in src
    # 2. A pin write goes through core's BATCH path (one POST /mods/policy per
    #    broker, under that broker's own lock), shared with #157's per-host pin
    #    editor rather than a second way to set the same field. #191 carved the
    #    ONE deliberate exception: an enforcing target's write must carry
    #    X-Webterm-Admin, which the shared helper cannot, so mod-sync owns an
    #    admin variant of the same wire -- exactly one occurrence of the route
    #    string, inside that variant (pinned further in
    #    test_mod_sync_admin_class_detect_prompt_header_outcome), and every
    #    non-admin write still rides saveModPins.
    assert "saveModPins(" in code
    assert code.count("'/mods/policy'") == 1, \
        "mod-sync POSTs /mods/policy itself ONLY in its #191 admin variant"
    # A peer's catalog is read through the SHARED fetcher, so this mod and the
    # #157 pane can never disagree about what a broker serves.
    assert "await fetchModCatalog(host)" in code
    assert "modCatalogCache.get(host.id)" in code
    # A pinned-ON mod implies its dependencies ON transitively, resolved on the
    # TARGET over ITS catalog -- so "that broker's default already matches, leave
    # it unpinned" is not always enough: a mod we want OFF can be dragged ON by a
    # pinned-ON dependent, including one that only exists on that build. The plan
    # mirrors the implied-pin resolution and writes an explicit pin where the
    # implication contradicts the wish (an explicit pin always wins).
    assert "modPolicyImplied(" in code
    # Minimal pins, and self-cleaning: where a broker's own default already lands
    # where we want, an existing pin is CLEARED rather than left locking that
    # broker's checkbox forever.
    assert "writes[m.id] = null;" in code
    # Every target is re-resolved by id at write time: hostFetch(null, ...)
    # silently resolves against our OWN origin with no token, so a host removed
    # while a dialog was open must never reach it.
    assert "hostById(plan.hostId)" in code
    # Mod code is NEVER shipped over the wire (#158 scope A): no file API, no
    # mod-store deposit, no new endpoint.
    for forbidden in ("ctx.file", "ctx.serverStore", "mod-store"):
        assert forbidden not in code, \
            f"mod-sync must not ship mod code / add storage: {forbidden!r}"
    # --- defects an adversarial review of the diff found, kept fixed ---
    # Retry must repeat the SAME operation. Rebuilding with a hardcoded
    # lockAll=false silently downgrades a "Lock every mod" push to a minimal
    # one -- and in minimal mode every pin whose target default already agrees
    # becomes an explicit CLEAR, so the "retry" would undo the locks the user
    # asked for and then report success.
    assert "lockAll: !!lockAll" in code
    assert "planFor(h, !!r.lockAll)" in code
    # ...and a retry must not destroy the row's undo baseline: keep the EARLIEST
    # prior value per id (older map last, since a later Object.assign arg wins)
    # and undo the union of what both attempts wrote.
    assert "out.priorPolicy, r.priorPolicy" in code
    assert "r.wrote, out.wrote" in code
    # hostFetch's deadline stops at the response HEADERS, so a peer that answers
    # then stalls its body would hang forever -- and the push walks its targets
    # sequentially, so one such peer wedges the whole fan-out. Same guard, and
    # reason, as fetchHostState.
    assert "new AbortController()" in code
    assert "timeoutMs: 0" in code
    # Adopting a PEER's setting value needs a DOMAIN check, not just a scalar
    # shape one: mods validate read-through and silently fall back, so an
    # out-of-domain value would be reported as applied and then ignored.
    assert "function acceptedBy(entry, v)" in src
    assert "acceptedBy(byKey.get(s.key), there)" in code
    # Self-targeting fails CLOSED. brokerId is only known once a host has been
    # polled, so the identity test alone lets an unpolled alias through and we
    # would write THIS broker's own policy under a peer's name.
    assert "=== window.location.origin" in code
    # A peer's catalog is untrusted: modPolicyImplied must get the SANITIZED
    # array, or one null element throws and takes the whole preview down.
    assert "modPolicyImplied(rec.mods" not in code
    assert "modPolicyImplied(cat, after)" in code
    # A POST that times out may still have committed, so the undo baseline is
    # recorded BEFORE the attempt rather than only on a confirmed success.
    assert "out.wrote = Object.assign({}, plan.setObj);" in code
    # Ships in the served page, AFTER the mousemode mod (the last mod before it
    # in _MODS), and its CSS rides the mod-css splice.
    assert "id: 'mod-sync'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'mousemode'") \
        < INDEX_HTML.index("id: 'mod-sync'")
    assert ".modsync-actions" in INDEX_HTML


def test_mod_sync_never_adopts_a_mod_this_build_does_not_have():
    # #163 out-of-scope guard: mod-sync carries pins and settings, NEVER mod
    # code, and that has to survive one broker having an installed `x-` mod the
    # other has never seen.
    #
    # adopt walks OUR OWN window.__mods.registered (via localMods), never the
    # peer's catalog, so an id we do not have has no local switch to flip and is
    # structurally unadoptable -- rather than being "handled" somewhere later.
    # planFor is the mirror image: it skips a mod absent from the peer's catalog
    # in BOTH minimal and lockAll modes, so no pin naming a mod that broker does
    # not serve is ever written.
    src = (BROKER_DIR / "mods" / "mod-sync" / "mod-sync.js").read_text(
        encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    adopt = code[code.index("async function adoptPlan("):
                 code.index("function applyAdopt(")]
    assert "for (const m of localMods())" in adopt
    # The ONLY iteration over the peer's catalog in adopt builds the lookup map;
    # nothing walks it to produce a row, so a peer-only id cannot become one.
    assert "for (const m of cat) byId.set(m.id, m);" in adopt
    assert adopt.count("for (const m of cat)") == 1
    assert "of byId" not in adopt and "of rec.mods" not in adopt
    # localMods IS window.__mods.registered, so "absent from registered" and
    # "not adoptable" are the same statement.
    assert "for (const m of window.__mods.registered) {" in code
    # A mod we have and the peer does not is REPORTED, not silently dropped: an
    # omitted row reads as agreement about a mod that broker never heard of.
    assert "note: 'not installed there'" in code
    assert "} else if (r.action === 'missing') {" in code
    # ...including when there is nothing else to preview, where the skip lines
    # are never rendered at all.
    assert "' Left alone: '" in code
    # appendLines truncates at DETAIL_MAX from the FRONT, so the new skip rows
    # must not be able to push the writes this dialog exists to confirm out of
    # view: the two lists are built separately and concatenated writes-first.
    assert "const lines = writes.concat(skips);" in code
    assert "if (!writes.length) {" in code
    # planFor's half, unchanged and pinned here so it cannot rot: a mod absent
    # from the peer's catalog is skipped before any pin is computed, in BOTH
    # modes (one shared loop), and reported as a row.
    plan_for = code[code.index("async function planFor("):
                    code.index("async function readState(")]
    assert "if (!cat) continue;" in plan_for
    assert "note: 'not installed on that broker'" in plan_for


def test_mod_sync_admin_class_detect_prompt_header_outcome():
    # #191: a peer with `admin_token` configured 403s admin_required on the
    # POST /mods/policy push wire. mod-sync (a) DETECTS that per target from
    # the peer's /info alone -- the `admin` key fetchModCatalog carries in the
    # shared catalog record; absence is the old/non-enforcing-build signal,
    # never an error, and NEVER a probe POST (the probe IS the write);
    # (b) prompts ONCE per push for the admin token; (c) sends it as
    # X-Webterm-Admin on the policy writes to enforcing targets only, while
    # every other target keeps today's wire byte for byte; (d) renders an
    # admin refusal as an AUTH outcome distinct from a network failure.
    src = (BROKER_DIR / "mods" / "mod-sync" / "mod-sync.js").read_text(
        encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    # (a) detection rides the /info-derived catalog record and the pane's
    # routes-aware predicate (guarded, like _pin: newer than ctxVersion 1),
    # and issues NO request of its own -- no fetch, no method, no probe.
    assert "const POLICY_ROUTE = '/mods/policy';" in code
    assert code.count("'/mods/policy'") == 1  # the const is the only literal
    detect = code[code.index("function adminEnforcing(rec)"):
                  code.index("async function saveModPinsAdmin(")]
    assert "rec.admin" in detect
    assert "typeof _adminRequiredFor === 'function'" in detect
    assert "_adminRequiredFor(info, POLICY_ROUTE)" in detect
    assert "if (!info) return false;" in detect     # absent key = today's wire
    assert "hostFetch" not in detect and "method" not in detect
    assert "plan.adminRequired = adminEnforcing(rec);" in code
    # (b) ONE prompt per push, only when an enforcing target is actually
    # getting a policy write (the settings half rides /state, which is not
    # admin-gated); a password-type input so the token is never readable off
    # a shared or streamed screen; Retry and Undo are pushes of their own and
    # ask again.
    assert "async function promptAdminToken(" in code
    assert "input.type = 'password';" in code
    assert "input.autocomplete = 'off';" in code
    assert "&& Object.keys(p.setObj).length" in code
    assert code.count("await promptAdminToken(") == 3  # push, retry, undo
    # (c) the header rides ONLY the admin variant, and that variant fires only
    # for a target that advertised `admin` AND with a token entered for this
    # push -- every other write (old builds included) still goes through the
    # shared saveModPins, wire byte-identical to before #191.
    assert code.count("'X-Webterm-Admin'") == 1
    admin_writer = code[code.index("async function saveModPinsAdmin("):
                        code.index("async function promptAdminToken(")]
    assert "'X-Webterm-Admin'" in admin_writer
    assert "hostFetch(host, POLICY_ROUTE, {" in admin_writer
    assert "plan.adminRequired && adminToken" in code
    assert "saveModPins(host, plan.setObj," in code   # the non-admin path stays
    # (d) an admin refusal is NAMED, never dressed as a network failure, and
    # both wordings are honest about whether a token was even entered.
    assert "=== 'admin_required'" in code
    assert "requires its admin token" in code
    # The token is held for the push alone: never parked on a plan or result
    # object (those outlive the push in the results pane), never in the mods
    # pane's until-reload hold, and never in any page-readable store.
    assert ".adminToken" not in code
    assert "_adminHeld" not in code
    assert "localStorage" not in code
    # Ships in the served page.
    assert "promptAdminToken" in INDEX_HTML


# --------------------------------------------------------------------------- #
# workspaces mod (#148)
# --------------------------------------------------------------------------- #

def test_workspaces_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "workspaces"
    js = mod_dir / "workspaces.js"
    css = mod_dir / "workspaces.css"
    manifest = mod_dir / "mod.json"
    assert js.is_file() and css.is_file() and manifest.is_file()
    # Deliberately NO help.md: wiki/Workspaces.md already owns the `workspaces`
    # corpus slug (build_full_corpus raises BuildError on a duplicate) and five
    # other wiki pages link to it, so a mod help.md would collide or duplicate.
    assert not (mod_dir / "help.md").exists()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "workspaces"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "workspaces.js"
    assert meta["styles"] == ["workspaces.css"]
    assert "help" not in meta
    assert "mods/workspaces/workspaces.js" in ui._MODS
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'workspaces'" in src
    assert "ctxVersion: 1" in src
    # Tiers match the order-sensitive _EXPECTED_TIERS guard above.
    assert "tiers: ['window', 'taskbar', 'settings']" in src
    # Default ON — no behaviour change on upgrade. Asserted so flipping it off
    # becomes a deliberate, reviewed change.
    assert "defaultEnabled" not in src
    # It owns its three settings by READ-THROUGH onto the same synced blob (the
    # #126 termfont pattern), so an upgrading user's stored value survives.
    assert "ctx.settings.select('wsLabelMode'" in src
    assert "ctx.settings.boolean('hideTaskbarOtherWs'" in src
    assert "ctx.settings.boolean('hideWsPager', false" in src        # #162
    # One heading for the block: ctx.settings.* gives each control its own
    # .set-section and they are appended in creation order, so the title on the
    # FIRST one labels all three (and only one may carry it).
    assert src.count("title: 'Workspaces'") == 1
    # ...and they are gone from core's normalizeSettings, which would otherwise
    # write a default into the synced blob behind the mod's back.
    model = (BROKER_DIR / "55_js_settings_model.js").read_text(encoding="utf-8")
    assert "s.wsLabelMode" not in model
    assert "s.hideTaskbarOtherWs" not in model
    assert "s.hideWsPager" not in model
    # The pager is created at init and removed on unload (the #clock-chip
    # pattern); #park stays CORE, because parkWindow is a tiling API.
    assert "pager.id = 'ws-pager'" in src
    assert 'id="ws-pager"' not in (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    assert 'id="park"' in (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    assert "#park { display: none; }" in \
        (BROKER_DIR / "13_css_tiling.css").read_text(encoding="utf-8")
    # Ships in the served page.
    assert "id: 'workspaces'" in INDEX_HTML


def test_core_is_single_desktop():
    # #148: core owns ONE desktop. No core fragment may name a workspace symbol
    # or reach for the pre-#148 multi-workspace shape -- the ONLY exception is
    # 57's one-time legacy migration, which exists precisely to erase it.
    banned = (
        "activeWorkspace(", "renderWorkspaces(", "switchWorkspace(",
        "addWorkspace(", "sendWindowToWorkspace(", "workspaceIndexForKey(",
        "applyWorkspaceVisibility(", "applyTaskbarWorkspace(",
        "adoptFloatWorkspace(", "floatWsMap(", "windowWsId(", "setWindowWs(",
        "newWorkspace(", "wsLabelMode", "hideTaskbarOtherWs", "hideWsPager",
        ".workspaces[", ".activeWs", "wsIndex",
    )
    for name in ui._ORDERED:
        if not name.endswith(".js"):
            continue
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines()
                          if not l.strip().startswith("//"))
        if name == "57_js_tiling_model.js":
            # The migration reads L.workspaces / L.activeWs exactly once, to
            # delete them. Scope the check to everything else in the file.
            i = code.index("function migrateLegacyWorkspaces")
            j = code.index("function reconcileLayout")
            code = code[:i] + code[j:]
        for sym in banned:
            assert sym not in code, f"{name} still names {sym!r} -- core is single-desktop"


def test_layout_columns_array_is_rewritten_in_place():
    # reconcileLayout used to REPLACE L.columns. A caller that resolved
    # `L.columns` and then called a helper which itself calls getLayout()
    # (visibleColumns and storageColIndex both do) spliced into the array
    # reconcile had just orphaned, and its column vanished silently --
    # layoutAddColumn hit this on the very first insert. Pin the in-place
    # rewrite, and pin that the mutators resolve their index into a local
    # BEFORE touching L.columns.
    model = (BROKER_DIR / "57_js_tiling_model.js").read_text(encoding="utf-8")
    recon = model[model.index("function reconcileLayout"):
                  model.index("function getLayout")]
    # CODE only -- the comment right there states the rule by quoting it.
    recon = "\n".join(l for l in recon.splitlines()
                      if not l.strip().startswith("//"))
    assert "L.columns = cleanCols" not in recon, \
        "reconcileLayout must not replace the columns array"
    assert "L.columns.length = 0;" in recon and "L.columns.push(c);" in recon
    mut = (BROKER_DIR / "58_js_layout_mutators.js").read_text(encoding="utf-8")
    assert "L.columns.splice(storageColIndex(" not in mut, \
        "resolve the storage index into a local before touching L.columns"


def test_visible_and_storage_column_indexes_are_not_confused():
    # Two index spaces exist now. findKeyInLayout's colIndex is a STORAGE index;
    # everything the user points at is a VISIBLE one. The title-bar menu's column
    # items must go through visibleColIndex, never loc.colIndex.
    keys = (BROKER_DIR / "78_js_keybindings.js").read_text(encoding="utf-8")
    menu = keys[keys.index("function buildWindowMenu"):
                keys.index("function buildCtxMenu")]
    assert "visibleColumns()" in menu
    for bad in ("moveColumn(loc.colIndex", "dragDropNewColumn(win.id, loc.colIndex",
                "loc.colIndex > 0", "loc.colIndex < ncols"):
        assert bad not in menu, f"{bad!r} mixes the storage index into a screen position"
    model = (BROKER_DIR / "57_js_tiling_model.js").read_text(encoding="utf-8")
    assert "function visibleColumns" in model
    assert "function visibleColIndex" in model
    assert "function storageColIndex" in model


def test_workspaces_off_leaves_no_window_unreachable():
    # The whole point of the #148 model: nothing is ever MOVED for a workspace,
    # so with the mod absent every column is simply drawn. Pin the three things
    # that guarantee it.
    model = (BROKER_DIR / "57_js_tiling_model.js").read_text(encoding="utf-8")
    vis = model[model.index("function visibleColumns"):
                model.index("function visibleColIndex")]
    # 1. No filter -> the storage array itself, untouched.
    assert "if (!_columnFilter) return L.columns;" in vis
    # 2. A THROWING filter fails OPEN (shows everything), never blacks out.
    assert "return L.columns;" in vis.split("catch")[1]
    # 3. Dropping the filter relayouts, so the columns reappear immediately.
    reg = model[model.index("function registerColumnFilter"):
                model.index("function visibleColumns")]
    assert reg.count("requestRelayout();") == 2
    # And the mod's teardown clears only the presentation it applied -- it must
    # never touch the store, or a disable would be destructive.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    teardown = ws[ws.rindex("ctx.onUnload(function () {"):]
    for bad in ("delete ", "splice(", "= null", "savePrefs("):
        assert bad not in teardown, \
            f"the workspaces teardown must not mutate state ({bad!r})"


def test_hiding_the_pager_leaves_workspaces_reachable():
    # #162: `hideWsPager` hides ONLY #ws-pager. Since the pager's dots were the
    # sole pointer route to rename/remove (and, in floating mode, to another
    # workspace at all), the setting only ships alongside the two things that
    # keep everything reachable without it.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    # 1. ONE node, toggled by display -- never remove-and-recreate: the unload
    #    closes over the original node, so a recreate would leave a live pager
    #    behind after a hide/show cycle plus a disable.
    vis = ws[ws.index("function applyPagerVisibility"):
             ws.index("let _wsDirty = false;")]
    assert "pager.style.display = hide ? 'none' : '';" in vis
    assert "pager.remove()" not in vis and "createElement" not in vis
    # ...and the pager's removal is registered BEFORE anything that can throw:
    # initMod rolls a failed init back with the unloads it got, so a later throw
    # would otherwise strand the node in the taskbar with no mod behind it.
    assert ws.index("ctx.onUnload(function () { pager.remove(); });") \
        < ws.index("function applyPagerVisibility")
    # ...and display, not an emptied node: #taskbar is a flex row with a gap, so
    # a present-but-empty pager would still eat a gap slot (the same trap core
    # fixed for its neighbour with `#host-status:empty { display: none; }`).
    root_css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    bar = root_css[root_css.index("#taskbar {"):]
    assert "gap: 6px;" in bar[:bar.index("}")]
    # 2. The hover preview is a document.body child; hiding while the pointer
    #    rests on a dot (which a peer's change on the poll does for free) would
    #    strand it on screen. But NOT at init: hideWsPreview reads the
    #    `let _wsPreviewEl` declared further down, still in its temporal dead
    #    zone while init runs, and that ReferenceError would disable the whole
    #    mod on every load for anyone who had the setting on. (Nothing has
    #    hovered a dot at creation, so there is provably nothing to strand.)
    assert "if (hide && !atInit) hideWsPreview();" in vis
    assert ws.index("function applyPagerVisibility") < ws.index("let _wsPreviewEl = null;")
    # 3. Applied at CREATION, not only from onChange -- onChange never fires at
    #    init (the control entry is seeded with `last: read()`), so otherwise a
    #    hidden pager would reappear on every load.
    assert "applyPagerVisibility(true);" in vis, \
        "the stored value must be applied when the pager is created"
    assert "hidePager.onChange(function () { applyPagerVisibility(); });" in ws
    # 4. renderWsDots is left alone: clearing innerHTML without clearing
    #    dataset.sig would trip its churn guard and leave the pager permanently
    #    empty on re-show.
    dots = ws[ws.index("function renderWsDots"):ws.index("function buildWorkspaceMenu")]
    assert "container.dataset.sig = sig;" in dots
    assert "hideWsPager" not in dots and "style.display" not in dots
    # 5. Rename/remove also live on the desktop menu now, resolved BY ID at
    #    click time (the menu can outlive the workspace switch it was built on).
    contrib = ws[ws.index("ctx.registerDesktopMenuItems"):
                 ws.index("ctx.registerKeyActions")]
    assert "renameWorkspace(i)" in contrib and "removeWorkspace(i)" in contrib
    assert contrib.count("workspaceIndexById(curId)") == 2, \
        "both actions must re-resolve their workspace by id at click time"
    assert "activeWorkspaceIndex()" in contrib          # ...marked at build time
    # Same for the switcher rows: a menu is a frozen snapshot that can sit open
    # across a poll, and a peer removing a lower-indexed workspace would slide a
    # captured index onto its neighbour. (The pager dots may capture an index --
    # renderWsDots rebuilds them whenever the list changes; a menu never does.)
    assert "action: () => switchWorkspace(wi)" not in contrib
    assert "workspaceIndexById(wsId)" in contrib
    # 6. ...and that menu is offered in BOTH window modes, counting whatever the
    #    mode actually shows.
    assert "const tiling = !!(info && info.tiling);" in contrib
    assert "tiling ? workspaceColumns(ws.id).length" in contrib
    assert "countFloatingOnWs(ws.id)" in contrib


def test_legacy_workspace_blob_migrates_without_losing_a_column():
    # A pre-#148 blob's columns are CONCATENATED into L.columns (nothing is
    # dropped, so nothing becomes unreachable even if the mod never loads) and
    # the grouping is handed to the mod as ids only. Running before loadMods()
    # is safe precisely because it is non-destructive.
    model = (BROKER_DIR / "57_js_tiling_model.js").read_text(encoding="utf-8")
    mig = model[model.index("function migrateLegacyWorkspaces"):
                model.index("function reconcileLayout")]
    assert "columns.push(col)" in mig          # every legacy column is kept
    assert "held.has(col.id)" in mig           # ...and never duplicated
    assert "columnIds.push(col.id)" in mig     # grouping is IDS ONLY, no payload
    assert "delete L.workspaces;" in mig and "delete L.activeWs;" in mig
    assert "L.wsLegacy = {" in mig
    # It runs from reconcileLayout, i.e. on the first getLayout() -- before mods
    # load -- and is idempotent (an already-migrated blob has no `workspaces`).
    assert "if (!Array.isArray(L.workspaces)) return;" in mig
    recon = model[model.index("function reconcileLayout"):
                  model.index("function getLayout")]
    assert "migrateLegacyWorkspaces(L);" in recon
    assert recon.index("seedLayoutIdSeq(L);") < recon.index("migrateLegacyWorkspaces(L);")
    # The mod adopts it once and deletes it.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    assert "adoptLegacyWorkspaces(L, st);" in ws
    assert "delete L.wsLegacy;" in ws


def test_workspace_key_action_ids_are_preserved_verbatim():
    # User rebindings are stored BY ID, and DEFAULT_KEYBINDINGS (54) still
    # carries the defaults, so a rebinding survives the mod being toggled off
    # and comes back untouched. Renaming an id would silently drop it.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    ids = ["workspace-prev", "workspace-next"] + [f"workspace-{n}" for n in range(1, 6)]
    for i in ids:
        assert f"id: '{i}'" in ws, f"the {i!r} action id must be preserved verbatim"
    store = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    for i in ids:
        assert f"'{i}':" in store, \
            f"{i!r} must stay in DEFAULT_KEYBINDINGS so a rebinding survives a disable"
    # Every KEY_ACTIONS reader goes through the live accessor -- the old const
    # index was built at script eval, before any mod could contribute.
    keys = (BROKER_DIR / "78_js_keybindings.js").read_text(encoding="utf-8")
    assert "const CORE_KEY_ACTIONS = [" in keys
    assert "KEY_ACTION_BY_ID" not in keys
    assert "keyActionById(actionId)" in keys
    for rel in ("80_js_help_window.js", "82_js_settings_keys_hosts.js"):
        text = (BROKER_DIR / rel).read_text(encoding="utf-8")
        assert "for (const act of keyActions())" in text, \
            f"{rel} must read the live action list"


def test_desktop_and_menu_seams_present_in_loader():
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for fam in ("columnFilter:", "onColumnCreated:", "onPlaced:", "onForgotten:",
                "onReveal:", "onLayoutRender:", "onItemsRendered:",
                "interceptActivate:", "registerKeyActions:",
                "registerWindowMenuItems:", "registerDesktopMenuItems:"):
        assert fam in loader, f"missing ctx seam {fam!r}"
    # Every seam is register-and-remember, so a disable (or an initMod rollback
    # after a later throw) releases the slot exactly once.
    assert "function _modTrack(rec, off)" in loader
    assert loader.count("_modTrack(rec, register") >= 11
    # ...and every one is ONE slot: a second registration throws rather than
    # letting two mods silently fight over the desktop.
    for rel, fn in (("57_js_tiling_model.js", "registerColumnFilter"),
                    ("57_js_tiling_model.js", "registerColumnCreated"),
                    ("61_js_resize_gutters.js", "registerLayoutRendered"),
                    ("75_js_taskbar_hosts.js", "registerTaskbarItemsRendered"),
                    ("75_js_taskbar_hosts.js", "registerTaskbarActivateIntercept"),
                    ("78_js_keybindings.js", "registerWindowMenuItems"),
                    ("78_js_keybindings.js", "registerDesktopMenuItems")):
        text = (BROKER_DIR / rel).read_text(encoding="utf-8")
        body = text[text.index("function " + fn):]
        assert "ModConflictError" in body[:900], f"{fn} must refuse a second registration"


def test_menus_are_unchanged_without_a_contributor():
    # The menu seams are marked insertion points: with nobody registered they
    # push nothing, so the built-in menus render exactly as before. The one
    # visible consequence is that tiling mode's desktop menu can now be EMPTY,
    # which must not leave a leading separator.
    keys = (BROKER_DIR / "78_js_keybindings.js").read_text(encoding="utf-8")
    ctx = keys[keys.index("function buildCtxMenu"):
               keys.index("function buildTaskbarItemMenu")]
    assert "_pushMenuItems(_desktopMenuItems, items, { tiling: true });" in ctx
    assert "if (items.length) items.push({ sep: true });" in ctx
    # #162: the floating branch pushes too, into a SCRATCH array so core owns the
    # join -- renderMenu draws every {sep:true} it is handed, adjacent or not, so
    # an unconditional separator would leave a stray rule under "Minimize All
    # Windows" whenever no mod contributes, and a contributor's own boundary
    # separator would double core's.
    assert "_pushMenuItems(_desktopMenuItems, extra, { tiling: false });" in ctx
    assert "while (extra.length && extra[0].sep) extra.shift();" in ctx
    assert "while (extra.length && extra[extra.length - 1].sep) extra.pop();" in ctx
    assert "if (extra.length) {" in ctx
    # A throwing contributor must not take the whole context menu down.
    push = keys[keys.index("function _pushMenuItems"):]
    assert "catch (e)" in push[:400]

def test_mod_policy_pin_write_is_batched_and_shared():
    # #158: the pin write is ONE POST carrying the whole {set:{…}} map, so a bulk
    # apply lands under the broker's own lock in one round trip instead of N
    # writes leaving N partial states behind a failure. #157's per-host select
    # keeps its one-key entry point as a thin wrapper, so there is exactly one
    # write path for a mod pin.
    cp = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    assert "async function saveModPins(host, set, opts)" in cp
    assert "function saveModPin(host, id, pin)" in cp
    assert "return saveModPins(host, set)" in cp
    # A null host must fail closed rather than reach hostFetch, which would
    # resolve against our own origin and write THIS broker's policy.
    assert "if (!host) return { ok: false, error: 'no_host' };" in cp
    # Batch size is bounded by the same cap the broker enforces per call.
    assert "ids.length > MAX_MOD_POLICY_KEYS" in cp
    # The authoritative policy from the response still drives the cache, so a
    # refused or partial write can never leave the editor showing a phantom.
    assert "rec.policy = sanitizeModPolicy(j.policy);" in cp


def test_editor_serialized_fields_preserved():
    # The hard #83 requirement: every editor serialized field round-trips. They
    # live in the SHARED core serializeAppWindow (54), unchanged by the extraction.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    for field in ("filePath:", "wrap:", "lineNums:", "startDir:", "docs:",
                  "activeTab:", "agentsMdCwd:", "fileHostId:", "encoding:"):
        assert field in s54, f"serializeAppWindow lost the {field!r} editor field"


def test_window_kind_sites_use_registry():
    # The seven hardcoded appKind branches are replaced by registry lookups, and
    # the old per-kind branches are gone from each fragment they lived in.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "const kind = lookupWindowKind(win.appKind);" in s54
    for gone in ("win.appKind === 'task-manager'", "win.appKind === 'control-panel'",
                 "win.appKind === 'help'"):
        assert gone not in s54, f"old saveAppWindow branch survived: {gone!r}"

    # openAppWindow (the dispatcher) moved from the deleted 70 into core 54
    # (#83/S10) when the editor was extracted; it still dispatches via the registry
    # with the unknown-kind openNoteOrEditorWindow fallback, and the old per-kind
    # branches stay gone.
    assert "const kind = lookupWindowKind(appData.appKind);" in s54
    assert "return openNoteOrEditorWindow(appData);" in s54
    for gone in ("return openFileManagerWindow(appData)",
                 "return openTaskManagerWindow(appData)",
                 "return openControlPanelWindow(appData)",
                 "return openHelpWindow(appData)"):
        assert gone not in s54, f"old openAppWindow dispatch branch survived: {gone!r}"

    s73 = (BROKER_DIR / "73_js_window_runtime.js").read_text(encoding="utf-8")
    assert "kind.retainOnClose(rec)" in s73
    assert "rec.appKind === 'sticky-note'" not in s73

    s84 = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    # #167 split the per-record body out into _restoreOneAppWindow, which
    # null-checks the record up front, so the lookup no longer needs `rec &&`.
    assert "const kind = lookupWindowKind(rec.appKind);" in s84
    assert "if (!rec || rec.open === false) return;" in s84
    assert "=== 'task-manager'" not in s84   # the old explicit skip list is gone

    s76 = (BROKER_DIR / "76_js_launch_fullscreen.js").read_text(encoding="utf-8")
    assert "windowKindMenuList()" in s76


def test_delete_menu_item_is_registry_opt_in_not_denylist():
    # #186: the window context menu's "Delete note"/"Delete file" item used to be
    # gated by a denylist of four appKind values (task-manager/control-panel/
    # help/file-manager) — any kind added later inherited the item by default.
    # It is now an opt-in property (deleteLabel) on the registerWindowKind spec.
    keys = (BROKER_DIR / "78_js_keybindings.js").read_text(encoding="utf-8")
    for gone in ("win.appKind !== 'task-manager'",
                 "win.appKind !== 'control-panel'",
                 "win.appKind !== 'help'",
                 "win.appKind !== 'file-manager'"):
        assert gone not in keys, f"old denylist clause survived: {gone!r}"
    assert "lookupWindowKind(win.appKind)" in keys
    assert "_deleteKind.deleteLabel" in keys

    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "deleteLabel: spec.deleteLabel || null," in s54

    # Exactly the three persisted-doc kinds opt in.
    scratch = (BROKER_DIR / "mods/scratchpad/scratchpad.js").read_text(encoding="utf-8")
    sticky = (BROKER_DIR / "mods/sticky/sticky.js").read_text(encoding="utf-8")
    editor = (BROKER_DIR / "mods/editor/editor.js").read_text(encoding="utf-8")
    assert "deleteLabel: 'note'," in scratch
    assert "deleteLabel: 'note'," in sticky
    assert "deleteLabel: 'file'," in editor

    # No other kind ANYWHERE opts in — a Delete item would be a misleading
    # no-op on a window with nothing persisted (or, for file-manager, nothing
    # a "delete" would meaningfully discard). Swept over every fragment and
    # every mod file, not a fixed list, so a kind added later cannot opt in
    # unnoticed (the inverse of the denylist bug this replaced).
    allowed = {
        BROKER_DIR / "mods/scratchpad/scratchpad.js",
        BROKER_DIR / "mods/sticky/sticky.js",
        BROKER_DIR / "mods/editor/editor.js",
        BROKER_DIR / "54_js_app_windows_store.js",   # the registry itself
        BROKER_DIR / "78_js_keybindings.js",         # the guard that reads it
    }
    offenders = []
    for path in sorted(BROKER_DIR.rglob("*.js")):
        if "deleteLabel" in path.read_text(encoding="utf-8") \
                and path not in allowed:
            offenders.append(str(path.relative_to(BROKER_DIR)))
    assert offenders == [], (
        "deleteLabel appears outside the three persisted-doc kinds and the "
        f"registry/guard: {offenders}")
    assert "deleteLabel: 'control-panel'" not in s54
    assert "appKind: 'control-panel'" in s54   # still the one core built-in


# --------------------------------------------------------------------------- #
# app-icon system (#119)
# --------------------------------------------------------------------------- #

def test_app_icon_registry_present_in_core():
    # The single source of truth (APP_ICON_SVG) + its lookup helper live in core
    # 65 (the home of the only other inline control SVGs), so they're lexically
    # visible to 76/77 and the help mod once the page concatenates every fragment.
    s65 = (BROKER_DIR / "65_js_display_theming.js").read_text(encoding="utf-8")
    assert "const APP_ICON_SVG = {" in s65
    assert "function appIconSvg(" in s65
    # OWN-property lookup only, so an inherited key ('constructor'/'toString')
    # can never leak a non-string into the innerHTML sinks (codex hardening).
    assert "hasOwnProperty.call(APP_ICON_SVG, key)" in s65
    # Every canonical key (mod id; control-panel is the core built-in; clock/git/
    # help are help-only) has an SVG entry, quoted so hyphenated ids are valid.
    for key in ("editor", "sticky", "scratchpad", "file-manager", "task-manager",
                "clipboard", "aistatus", "help", "control-panel", "clock", "git"):
        assert f"'{key}':" in s65, f"APP_ICON_SVG missing key {key!r}"
    # The icons carry signature fills (not just currentColor) — the #119 departure
    # from the monochrome eyedropper/robot glyphs; the two focal fills are pinned.
    assert 'fill="#f7c948"' in s65   # editor pencil body (yellow)
    assert 'fill="#f5d90a"' in s65   # sticky note (yellow)
    # And it all reaches the served page.
    assert "const APP_ICON_SVG = {" in INDEX_HTML
    assert "function appIconSvg(" in INDEX_HTML


def test_launch_menu_items_carry_iconkey():
    # Every launcher drops its emoji label and declares iconKey; appMenuItems()
    # passes iconKey through untouched (renderMenu resolves it — the raw SVG never
    # travels on the item object, codex hardening). The label strings themselves
    # are NOT asserted (menu-order tests key on `id:` sentinels), so dropping the
    # emoji is free — but the iconKey wiring is what makes the SVG show.
    assert "iconKey: m.iconKey || ''" in INDEX_HTML
    for key in ("editor", "sticky", "scratchpad", "file-manager", "task-manager",
                "clipboard", "aistatus", "help", "control-panel"):
        assert f"iconKey: '{key}'" in INDEX_HTML, f"launcher missing iconKey {key!r}"
    # The old emoji-in-label form is gone from the two focal launchers.
    assert "'📄 Text editor'" not in INDEX_HTML
    assert "'📝 Sticky note'" not in INDEX_HTML


def test_render_menu_resolves_iconkey_to_trusted_svg():
    # renderMenu resolves it.iconKey through appIconSvg (which returns '' for a key
    # not in the registry) and injects ONLY that as innerHTML — no caller can route
    # arbitrary/user markup through the shared menu renderer (codex hardening). The
    # label stays textContent (the "labels are textContent only" rule).
    s77 = (BROKER_DIR / "77_js_context_menu.js").read_text(encoding="utf-8")
    assert "const iconSvg = it.iconKey ? appIconSvg(it.iconKey) : '';" in s77
    # #170 widened the branch to cover the text-glyph path; the SVG side is
    # unchanged (registry lookup -> innerHTML, label -> textContent).
    assert "if (iconSvg || iconText) {" in s77
    assert "ic.className = iconSvg ? 'ctx-icon' : 'ctx-icon ctx-icon-text';" in s77
    assert "ic.innerHTML = iconSvg;" in s77
    assert "lab.textContent = it.label;" in s77
    # renderMenu never injects a raw pre-resolved SVG — the only innerHTML value is
    # appIconSvg(it.iconKey), so the generic-HTML-sink is closed.
    assert "ic.innerHTML = it.icon" not in s77
    # The ctx-menu CSS gained the flex layout + icon sizing.
    css = (BROKER_DIR / "14_css_dragdrop.css").read_text(encoding="utf-8")
    assert "#ctx-menu .ctx-icon svg" in css
    assert "ctx-icon" in INDEX_HTML


def _render_menu_body():
    """The source text of renderMenu (77), from its `function renderMenu(` line
    to the next top-level `function ` at the same indent."""
    s77 = (BROKER_DIR / "77_js_context_menu.js").read_text(encoding="utf-8")
    start = s77.index("        function renderMenu(items, x, y) {")
    end = s77.index("\n        function ", start + 1)
    return s77[start:end]


def _css_rule(css, selector):
    """One CSS rule's declaration block, so a `in` assertion can't be satisfied
    by an unrelated rule further down the stylesheet (codex)."""
    start = css.index(selector + " {") + len(selector) + 2
    return css[start:css.index("}", start)]


def test_mod_window_kind_may_declare_a_text_glyph_icon():
    # #170: APP_ICON_SVG is a CLOSED table, so a mod-owned window kind can never
    # have an entry in it. Instead of ctx.registerAppIcon(svg) -- which the issue
    # rejects, because it would permanently forfeit renderMenu's "the only
    # innerHTML is our own markup" invariant for a decorative icon -- a kind may
    # declare a short TEXT glyph rendered with textContent.
    s65 = (BROKER_DIR / "65_js_display_theming.js").read_text(encoding="utf-8")
    assert "function appIconGlyph(" in s65
    # Non-strings (and '') resolve to '', so an item that declares nothing, or
    # declares junk, simply has no icon -- never a thrown render. The raw-length
    # gate is BEFORE the scan, so a megabyte from a store blob is rejected
    # without being copied on every repaint (codex).
    assert "if (typeof text !== 'string' || !text) return '';" in s65
    assert "const APP_GLYPH_RAW_MAX = 64;" in s65
    assert "if (text.length > APP_GLYPH_RAW_MAX) return '';" in s65
    # Capped at 12 CODE POINTS -- clears the longest RGI emoji sequence (10)
    # whole -- iterated with Array.from so a surrogate PAIR is never sliced in
    # half (which would emit a lone surrogate).
    assert "const APP_GLYPH_MAX = 12;" in s65
    assert "const cps = Array.from(cleaned);" in s65
    assert "cps.slice(0, APP_GLYPH_MAX).join('')" in s65
    # The drop-class: C0/C1 controls, every bidi control (an unterminated RLO in
    # the icon slot would reverse the reading order of the label beside it), the
    # invisible "bases" that would otherwise fake an empty-but-present icon, the
    # tag block, lone surrogates, and whitespace (so a glyph can neither pad a
    # row nor render blank as a fake separator).
    i = s65.index("const APP_GLYPH_DROP")
    drop = s65[i:s65.index("/gu;", i) + 4]
    for rng in ("0000-", "001F", "007F-", "009F",   # C0 / DEL + C1
                "061C", "200E", "200F",             # ALM, LRM, RLM
                "202A-", "202E",                    # embeddings + OVERRIDES
                "2066-", "2069",                    # isolates
                "206A-", "206F",                    # deprecated format controls
                "FFF9-", "FFFB",                    # interlinear annotation
                "E0000}-", "E007F}",                # LANGUAGE TAG block
                "D800-", "DFFF",                    # lone surrogates (u flag)
                "00AD", "034F", "115F", "1160",     # invisible "bases"
                "3164", "FFA0", "17B4", "17B5",
                "180B-", "180E", "200B-", "200C", "2060-"):
        assert rng in drop, f"APP_GLYPH_DROP misses {rng!r}"
    # The u flag is load-bearing twice: astral ranges, and making the surrogate
    # range match only UNPAIRED surrogates.
    assert drop.endswith("/gu;")
    assert "|" + chr(92) + "s/gu;" in drop, "APP_GLYPH_DROP must strip whitespace"
    # ZWJ, the variation selectors and the skin-tone modifiers SURVIVE, so a
    # multi-code-point emoji still renders as one grapheme, not its components.
    for kept in ("200D", "FE0F", "1F3FB"):
        assert kept not in drop, f"APP_GLYPH_DROP must not strip {kept}"
    # ...but a string of NOTHING BUT joiners/variation selectors is not a glyph:
    # it must resolve to '' rather than to a truthy, invisible icon that would
    # still open the icon column (codex).
    assert "const APP_GLYPH_VISIBLE = /[^" in s65
    assert "if (!APP_GLYPH_VISIBLE.test(cleaned)) return '';" in s65
    vis = s65[s65.index("const APP_GLYPH_VISIBLE"):]
    vis = vis[:vis.index("\n")]
    for ignorable in ("200D", "FE00-", "FE0F", "E0100}-", "E01EF}"):
        assert ignorable in vis, f"APP_GLYPH_VISIBLE misses {ignorable!r}"

    # renderMenu resolves the two into the {svg}/{text} tagged split the Help TOC
    # already uses: registry SVG wins, and only it is markup.
    s77 = (BROKER_DIR / "77_js_context_menu.js").read_text(encoding="utf-8")
    assert "const iconText = iconSvg ? '' : appIconGlyph(it.iconGlyph);" in s77
    assert "if (iconSvg) ic.innerHTML = iconSvg;" in s77
    assert "else ic.textContent = iconText;" in s77
    # THE invariant, checked structurally rather than by eyeballing one line:
    # every HTML sink inside renderMenu is either the '' wipe or the trusted
    # registry lookup. A glyph must never reach one. The sink list covers the
    # sideways spellings too (codex: matching only `.innerHTML =` would let
    # `outerHTML`/`insertAdjacentHTML` walk straight past this test).
    body = _render_menu_body()
    sinks = set(re.findall(r"\.(?:inner|outer)HTML\s*=\s*([^;]+);", body))
    assert sinks == {"''", "iconSvg"}, f"renderMenu grew an HTML sink: {sinks}"
    for sink in ("insertAdjacentHTML", "createContextualFragment",
                 "document.write", "srcdoc", "DOMParser"):
        assert sink not in body, f"renderMenu grew an HTML sink: {sink}"
    for never in ("ic.innerHTML = iconText", "ic.innerHTML = it.iconGlyph",
                  "innerHTML = appIconGlyph"):
        assert never not in s77

    # appMenuItems passes the kind's declared glyph through untouched (like
    # iconKey: the item object never carries resolved markup). The launch menu's
    # repaint fingerprint takes the NORMALIZED value, so it tracks what actually
    # renders and can never be handed a non-string to String() (codex).
    s76 = (BROKER_DIR / "76_js_launch_fullscreen.js").read_text(encoding="utf-8")
    assert "iconGlyph: m.iconGlyph || ''" in s76
    assert "appIconGlyph(it.iconGlyph)," in s76
    assert "it.iconGlyph || ''," not in s76

    # The glyph sits in the same fixed 15px box as an SVG icon, clipped and
    # bidi-isolated, so an oversized/stacked/RTL glyph can't grow a menu row,
    # displace the label, or reorder it. Every containment property is stated on
    # THIS rule rather than inherited from `.ctx-icon` (codex).
    css = (BROKER_DIR / "14_css_dragdrop.css").read_text(encoding="utf-8")
    rule = _css_rule(css, "#ctx-menu .ctx-icon.ctx-icon-text")
    for decl in ("display: inline-flex;", "flex: 0 0 15px;",
                 "box-sizing: border-box;", "width: 15px;", "max-width: 15px;",
                 "height: 15px;", "max-height: 15px;", "overflow: hidden;",
                 "white-space: nowrap;", "unicode-bidi: isolate;",
                 "direction: ltr;"):
        assert decl in rule, f".ctx-icon-text misses {decl!r}"

    # And it all reaches the served page.
    for needle in ("function appIconGlyph(", "iconGlyph: m.iconGlyph || ''",
                   "ctx-icon-text", "unicode-bidi: isolate;"):
        assert needle in INDEX_HTML, f"#170 glyph path missing from page: {needle!r}"


def test_help_toc_resolves_svg_app_icons():
    # The Help window's rail + section headers prefer the SVG app icon keyed by the
    # section's owning mod id (e.owner == the corpus per-section `mod` field). The
    # icon is stored TAGGED as { svg } (trusted registry, innerHTML) vs { text }
    # (emoji/'•' fallback, textContent), so mod-supplied secIcon can never reach
    # innerHTML even if it looks like markup (codex hardening). Wiki/un-owned
    # sections get '' from appIconSvg and take the text path.
    src = (BROKER_DIR / "mods" / "help" / "help.js").read_text(encoding="utf-8")
    assert "appIconSvg(e.owner)" in src
    assert "{ svg: svg }" in src
    assert "e.secIcon || helpSectionIcon(e.slug)" in src
    assert "function helpSetSectionIcon(" in src
    assert "el.innerHTML = icon.svg;" in src
    assert "helpSetSectionIcon(ric," in src
    assert "helpSetSectionIcon(sic," in src
    assert "appIconSvg(e.owner)" in INDEX_HTML
    # help.css sizes the injected SVG in both the rail column and header box.
    hcss = (BROKER_DIR / "mods" / "help" / "help.css").read_text(encoding="utf-8")
    assert ".help-rail-ic svg" in hcss
    assert ".help-section-icon svg" in hcss


def test_help_pre_block_renders_as_real_pre():
    # The corpus parser emits a 'pre' block for fenced code: one 'code' span
    # carrying the WHOLE block, real '\n' + original indentation intact
    # (no longer folded to a single space-joined 'p'). helpRenderFrags must
    # render that as an actual <pre>, not a <div class="help-b-p">
    # paragraph -- a div would let normal HTML whitespace collapsing eat
    # every newline (a live regression in the working tree without this
    # branch).
    src = (BROKER_DIR / "mods" / "help" / "help.js").read_text(encoding="utf-8")
    body = _frag_fn(src, "function helpRenderFrags(")
    assert "blk.t === 'pre'" in body
    assert "document.createElement('pre')" in body
    assert "help-b help-b-pre" in body
    # Text still goes through helpAppendHighlighted (text nodes + <mark>
    # only, never innerHTML), so search-term highlighting still works
    # inside a code sample and the XSS boundary guarded by
    # test_help_corpus.py's test_help_render_path_has_no_innerhtml is
    # unchanged.
    assert "helpAppendHighlighted(pre, sp.v, q)" in body
    assert "blk.t === 'pre'" in INDEX_HTML
    assert "help-b help-b-pre" in INDEX_HTML

    # And the CSS keeps those newlines visible: monospace + preserved
    # whitespace + its OWN horizontal scroll (not a window-stretching
    # overflow) for something like a long curl line. The rule lives beside
    # the other .help-b-* rules in the Help MOD's css -- 12_css_help.css is
    # a legacy filename that kept only the shared resize handles when #78
    # extracted every Help-specific selector out of core.
    css = (BROKER_DIR / "mods" / "help" / "help.css").read_text(encoding="utf-8")
    rule = _css_rule(css, ".app-help .help-b-pre")
    assert "font-family: monospace;" in rule
    assert "white-space: pre;" in rule
    assert "overflow-x: auto;" in rule
    assert ".app-help .help-b-pre" in INDEX_HTML


def test_mod_contributed_help_blocks_admit_pre():
    # The loader sanitizes a mod's help cards against a block-type whitelist and
    # coerces anything unknown to 'p'. That whitelist and the schema comment
    # documenting it must agree: publishing 'pre' in the contract while the
    # sanitizer rewrites it to 'p' would hand mods a block type that silently
    # renders as a nowrap <code> run-on line -- the exact regression the wiki
    # corpus side was restructured to avoid.
    # #194 moved the sanitizer into 86d; the CONTRACT it must agree with is the
    # ctx.registerHelpCards doc comment, which stayed with makeCtx in 86. That
    # split is exactly why this test is worth keeping: the whitelist and the
    # published schema now live in two files.
    assert "const _HELP_BLOCK_TYPES = { p: 1, bullet: 1, sub: 1, pre: 1 };" \
        in _help_cards_src()
    assert "block = { t:'p'|'bullet'|'sub'|'pre', spans:[span] }" in _loader_src()


def test_chip_icons_use_registry():
    # #119 follow-up: the aistatus + clipboard taskbar chips and git's title-bar
    # button render the SAME registry SVGs (via appIconSvg) instead of an emoji /
    # ⎇ glyph, so the chrome matches the (+) menu + Help TOC.
    ais = (BROKER_DIR / "mods" / "aistatus" / "aistatus.js").read_text(encoding="utf-8")
    assert "appIconSvg('aistatus')" in ais
    # The status text rides in its own span so renderChip no longer clobbers the
    # icon with chip.textContent.
    assert "chipText.textContent = txt;" in ais
    assert "chip.textContent = txt;" not in ais

    clip = (BROKER_DIR / "mods" / "clipboard" / "clipboard.js").read_text(encoding="utf-8")
    assert "appIconSvg('clipboard')" in clip
    assert "chip.textContent = '📋'" not in clip   # the emoji glyph is gone

    git = (BROKER_DIR / "mods" / "git" / "git.js").read_text(encoding="utf-8")
    assert "appIconSvg('git')" in git
    assert "gitBtn.textContent = '⎇'" not in git   # the ⎇ glyph is gone

    # All three reach the served page.
    for needle in ("appIconSvg('aistatus')", "appIconSvg('clipboard')", "appIconSvg('git')"):
        assert needle in INDEX_HTML, f"chip icon missing from served page: {needle!r}"
    # Each SVG is sized in its own mod stylesheet.
    assert "#aistatus-chip .aistatus-chip-ic svg" in \
        (BROKER_DIR / "mods" / "aistatus" / "aistatus.css").read_text(encoding="utf-8")
    assert "#clipboard-chip svg" in \
        (BROKER_DIR / "mods" / "clipboard" / "clipboard.css").read_text(encoding="utf-8")
    assert ".btn-git svg" in \
        (BROKER_DIR / "mods" / "git" / "git.css").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# file-manager mod (#84 / S11)
# --------------------------------------------------------------------------- #

def test_filemanager_symbols_removed_from_core_fragments():
    # The file manager is now a mod (#84/S11): its built-in registration is gone
    # from core 54 and its launcher from core 76 — both moved into
    # mods/file-manager/. The core fragment 71_js_file_manager.js is DELETED.
    assert not (BROKER_DIR / "71_js_file_manager.js").exists()
    assert "71_js_file_manager.js" not in ui._ORDERED
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'file-manager'",
                                       "function openFileManagerWindow",
                                       "return openFileManagerWindow(d)"),
        "76_js_launch_fullscreen.js": ("function launchFileManager",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The moved builder + launcher are present + reachable in the served page (they
    # ship in mods/file-manager/ as hoisted functions).
    for sym in ("function openFileManagerWindow", "function launchFileManager"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_openappwindow_fallback_does_not_coerce_unknown_kinds():
    # mods-off safety (#84): a persisted file-manager record must NOT be coerced
    # into a sticky note by the unknown-kind fallback (which would mis-render it
    # AND rewrite its stored record, destroying it). openAppWindow's fallback only
    # builds the note/editor for the note/editor kinds (+ a legacy record with no
    # appKind); any other unregistered kind returns null, leaving its record intact.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "if (ak && ak !== 'sticky-note' && ak !== 'text-editor') return null;" in s54
    # The note/editor builder is still the fallback for the kinds it owns.
    assert "return openNoteOrEditorWindow(appData);" in s54


def test_filemanager_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "file-manager"
    fm_js = mod_dir / "file-manager.js"
    manifest = mod_dir / "mod.json"
    assert fm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "file-manager"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "file-manager.js"
    assert "mods/file-manager/file-manager.js" in ui._MODS
    src = fm_js.read_text(encoding="utf-8")
    # Registers the file-manager mod + contributes the file-manager window kind
    # through ctx.registerWindowKind, reusing the shared core serializer + builder.
    assert "registerMod(" in src
    assert "id: 'file-manager'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'file-manager'" in src
    assert "serialize: serializeAppWindow" in src
    assert "return openFileManagerWindow(d)" in src
    assert "return launchFileManager()" in src
    # File I/O (incl. the DESTRUCTIVE delete + upload) rides ctx.file (#82): the
    # mod stashes ctx.file and every /file/* call flows through fmFile() — NO direct
    # fileApiPost AND no raw upload fetch (hostFetch) survives in the mod.
    assert "fmFile.cap = ctx.file;" in src
    assert "fmFile().list(" in src
    assert "fmFile().read(" in src
    assert "fmFile().delete(" in src
    assert "fmFile().upload(" in src
    assert "fileApiPost(" not in src, "file-manager mod must route I/O through ctx.file"
    assert "hostFetch(" not in src, "the raw upload fetch must be gone"
    # Ships in the served page, AFTER the help mod and BEFORE the editor mod (so the
    # (+) menu lists File manager right after the core built-ins, ahead of the
    # text-editor + sticky-note mods).
    assert "id: 'file-manager'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'file-manager'")
    assert INDEX_HTML.index("id: 'file-manager'") < INDEX_HTML.index("id: 'editor'")


def test_filemanager_serialized_fields_preserved():
    # The hard #84 requirement: every file-manager serialized field round-trips.
    # They live in the SHARED core serializeAppWindow (54), unchanged by the
    # extraction (the mod reuses it as its `serialize`).
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    for field in ("fmLeft:", "fmRight:", "fmLeftHostId:", "fmRightHostId:",
                  "fileHostId:"):
        assert field in s54, f"serializeAppWindow lost the {field!r} file-manager field"


# --------------------------------------------------------------------------- #
# ctx.file capability (#82 / S9)
# --------------------------------------------------------------------------- #

def test_file_capability_present():
    # #82 (S9): the ctx.file wrapper over /file/* + its host-routing helpers ride
    # in the served loader. These are the symbols the Playwright acceptance (and
    # any S10/S11 mod) depends on. ctxVersion stays 1 (additive capability).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # The capability object + its five methods, on the per-mod ctx.
    for sym in ("file: {",
                "read: function (path, opts)",
                "write: function (path, content, opts)",
                "list: function (path, opts)",
                "'delete': function (path, opts)",
                "upload: function (path, contentB64, opts)"):
        assert sym in loader, f"missing ctx.file method: {sym!r}"
    # Each method targets the matching /file/* route, wrapped here.
    for route in ("'/file/read'", "'/file/write'", "'/file/list'",
                  "'/file/delete'", "'/file/upload'"):
        assert route in loader, f"ctx.file does not wrap route {route!r}"
    # The host-routing helpers: fail-closed resolution + the synthetic error.
    for sym in ("function _modFileHost", "function _modFileApi",
                "error: 'host_not_found'"):
        assert sym in loader, f"missing ctx.file host-routing symbol: {sym!r}"
    # Routing reuses the EXISTING core helpers (no parallel host logic).
    for sym in ("hostById(hostId)", "return localHost();",
                "fileApiPost(route, body, host)"):
        assert sym in loader, f"ctx.file must reuse core host helper: {sym!r}"
    # ctxVersion is unchanged — ctx.file is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modFileApi", "file: {", "error: 'host_not_found'"):
        assert sym in INDEX_HTML, f"ctx.file missing from served page: {sym!r}"


def test_server_store_capability_present():
    # #124: the ctx.serverStore wrapper over /mod-store/<modId> + its transport
    # helper ride in the served loader. The durable, cross-browser twin of
    # ctx.storage; the scratchpad mod depends on it. ctxVersion stays 1 (additive).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # The capability object + its three methods. Each takes an additive opts arg
    # (#65: opts.host routing + set()'s opts.purgeRevisions); the positional
    # params are unchanged, so a #124 caller that omits opts is byte-compatible.
    for sym in ("serverStore: {",
                "get: function (opts)",
                "set: function (value, baseRev, opts)",
                "getRevision: function (n, opts)"):
        assert sym in loader, f"missing ctx.serverStore method: {sym!r}"
    # The transport helper targets /mod-store/<modId>, resolves the target broker
    # via _modStoreHost (local by default, so #124 callers are unchanged; #65
    # publish-to-all routes to a specific host id and FAILS CLOSED on an unknown
    # one), and set() auto-attaches the core lease id so the active browser passes.
    for sym in ("function _modStoreApi", "function _modStoreHost",
                "'/mod-store/'", "clientId: CLIENT_ID"):
        assert sym in loader, f"missing ctx.serverStore transport symbol: {sym!r}"
    # #174: EVERY method resolves the parsed body plus the HTTP `status` through
    # one helper. get() used to drop the status, which made "it refused our
    # password" (401) and "nothing published there" (200, value null) the same
    # empty body — unanswerable once a mod can read ANOTHER broker's store.
    assert "function _withStatus" in loader
    assert loader.count("_withStatus(r)") == 4      # the helper + its 3 callers
    # The status is applied AFTER the body, never before: a response body
    # carrying its own `status` must not overwrite the transport's, and a
    # cross-broker read makes that body untrusted input.
    assert "Object.assign({}, j, { status: r.status })" in loader
    assert "Object.assign({ status: r.status }, r.json)" not in loader
    # #192: set()'s opts.noHistory is a strict tri-state passthrough — an
    # explicit true/false rides the PUT body verbatim, but omitted/null must
    # stay OFF the wire (a retry/rebase that drops the key must never
    # un-stick a flagged record, which is the whole point of stickiness).
    assert "if (opts && typeof opts.noHistory === 'boolean') {" in loader
    assert "body.noHistory = opts.noHistory;" in loader
    # ctxVersion is unchanged — ctx.serverStore is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modStoreApi", "function _withStatus",
                "serverStore: {", "'/mod-store/'"):
        assert sym in INDEX_HTML, \
            f"ctx.serverStore missing from served page: {sym!r}"


def test_dialog_component_present():
    # #72 (Part A): the reusable styled dialog primitive + wrappers ship as the
    # new 69_js_dialog.js fragment, registered right after the file-dialog
    # fragment, and reach the served page; its CSS rides the shared dialogs
    # fragment by folding .app-dialog into the existing selector groups.
    assert "69_js_dialog.js" in ui._ORDERED
    assert ui._ORDERED.index("69_js_dialog.js") == \
        ui._ORDERED.index("68_js_app_windows_files.js") + 1
    assert (BROKER_DIR / "69_js_dialog.js").is_file()
    src = (BROKER_DIR / "69_js_dialog.js").read_text(encoding="utf-8")
    for sym in ("function openDialog", "function openTextPrompt",
                "function openConfirmDialog", "function openInfoModal"):
        assert sym in src, f"dialog fragment missing {sym!r}"
        assert sym in INDEX_HTML, f"dialog symbol missing from served page: {sym!r}"
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    for sel in (".app-dialog-overlay", ".app-dialog button.danger",
                ".app-dialog-rows"):
        assert sel in css, f"dialog CSS missing {sel!r}"
    assert ".app-dialog" in INDEX_HTML


def test_browse_pane_component_present():
    # #93: the reusable single browse-pane kernel ships as the new
    # 70_js_browse_pane.js fragment, ordered right after the dialog fragment,
    # and reaches the served page. BOTH consumers — the editor's openFileDialog
    # (core 68) and the file-manager mod — instantiate it, which is what proves
    # the two drifted directory-browsers were actually collapsed onto one.
    assert "70_js_browse_pane.js" in ui._ORDERED
    assert ui._ORDERED.index("70_js_browse_pane.js") == \
        ui._ORDERED.index("69_js_dialog.js") + 1
    frag = BROKER_DIR / "70_js_browse_pane.js"
    assert frag.is_file()
    src = frag.read_text(encoding="utf-8")
    assert "function createBrowsePane" in src
    assert "function createBrowsePane" in INDEX_HTML
    # Both consumers instantiate the component (the duplication is gone).
    dlg = (BROKER_DIR / "68_js_app_windows_files.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    assert "createBrowsePane(" in dlg, "the editor dialog must use createBrowsePane"
    assert "createBrowsePane(" in fm, "the file manager must use createBrowsePane"
    # The component is strictly host-/IO-agnostic: it must NOT reach for hosts,
    # the file API, or persistence — those are injected per-consumer via hooks.
    # Locking this keeps the editor dialog working mods-off and the FM's
    # fail-closed host semantics where they belong (the consumer).
    for banned in ("fileApiPost(", "hostFetch(", "saveAppWindow(",
                   "paneHost(", "fmFile("):
        assert banned not in src, \
            f"browse-pane component must stay I/O-agnostic, found {banned!r}"


def test_sticky_pin_button_present():
    # #95: a sticky note's titlebar gains an always-on-top (▲/△) toggle. The
    # feature is three wired edits — a per-note `pinned` flag (default true)
    # persisted by the shared serializer, a z-tier gate so an unpinned note drops
    # out of the high NOTE_Z_BASE tier, and the titlebar button itself — so lock
    # each edit at its source AND in the served page. (Real click/z-order
    # behavior is verified out of band via Playwright; this is the presence gate.)
    editor_js = (BROKER_DIR / "mods" / "editor" / "editor.js").read_text(
        encoding="utf-8")
    store_js = (BROKER_DIR / "54_js_app_windows_store.js").read_text(
        encoding="utf-8")
    poll_js = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(
        encoding="utf-8")
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")

    # The titlebar button (class hook + accessible title) ships in the editor mod
    # and reaches the served page.
    for needle in ("btn-pin", "always on top"):
        assert needle in editor_js, f"editor mod missing pin marker {needle!r}"
        assert needle in INDEX_HTML, f"pin marker missing from served page: {needle!r}"
    # The flag is persisted unconditionally by the shared serializer.
    assert "pinned: !!win.pinned" in store_js
    assert "pinned: !!win.pinned" in INDEX_HTML
    # The note z-tier is gated on the flag, still SCOPED to sticky notes so
    # `pinned` never becomes a cross-app z-capability.
    assert "win.pinned !== false" in poll_js
    assert "appKind === 'sticky-note'" in poll_js
    assert "win.pinned !== false" in INDEX_HTML
    # And the CSS styling/test hook exists.
    assert ".btn-pin" in css


def test_control_panel_floats_above_sticky_notes():
    # #98: the floating Control Panel rides a z-tier ABOVE the sticky-note
    # always-on-top tier (single floatZIndex source of truth, core 64).
    src = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(encoding="utf-8")
    assert "CONTROL_PANEL_Z_BASE" in src
    assert "appKind === 'control-panel'" in src
    assert "NOTE_Z_BASE = 90000" in src          # tier sits above the note tier
    assert "CONTROL_PANEL_Z_BASE" in INDEX_HTML   # and reaches the served page


def test_no_native_dialogs_in_served_page():
    # #89: the whole app routes every confirm/prompt through the styled dialog
    # component — NO native confirm()/prompt()/alert() survives anywhere in the
    # served page (core + every mod, assembled in one shot). The lookbehind skips
    # method calls / longer identifiers, and the styled wrappers are capitalized
    # (openConfirmDialog / openTextPrompt) so they never trip the lowercase match.
    import re
    assert not re.search(r"(?<![\w.])(confirm|prompt|alert)\s*\(", ui.INDEX_HTML), \
        "native confirm()/prompt()/alert() must not survive (use the styled dialog)"
    for sym in ("openConfirmDialog(", "openTextPrompt("):
        assert sym in ui.INDEX_HTML, f"styled dialog wrapper missing: {sym!r}"


def test_file_capability_richer_ops_present():
    # #72: ctx.file gains mkdir/copy/move/zip/unzip/stat and a recursive flag on
    # delete; ctxVersion stays 1 (additive). The SAME methods are mirrored in the
    # file-manager's fmFile() fallback so its I/O is identical mods on or off.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for src, label in ((loader, "loader ctx.file"), (fm, "fmFile fallback")):
        for sym in ("mkdir: function", "copy: function", "move: function",
                    "zip: function", "unzip: function", "stat: function",
                    "setattr: function"):                       # #96
            assert sym in src, f"{label} missing #72 method: {sym!r}"
        for route in ("'/file/mkdir'", "'/file/copy'", "'/file/move'",
                      "'/file/zip'", "'/file/unzip'", "'/file/stat'",
                      "'/file/setattr'"):                       # #96
            assert route in src, f"{label} does not wrap route {route!r}"
        # delete carries the recursive flag.
        assert "recursive: !!(opts && opts.recursive)" in src, \
            f"{label} delete missing recursive flag"
    # ctxVersion unchanged (additive capability).
    assert "ctxVersion: 1" in loader
    # And the new routes reach the served page.
    for route in ("'/file/copy'", "'/file/zip'", "'/file/stat'",
                  "'/file/setattr'"):                           # #96
        assert route in INDEX_HTML, f"#72 route missing from served page: {route!r}"


def test_file_capability_chunked_ops_present():
    # #108: ctx.file gains readChunk + the upload-session trio (uploadBegin/
    # uploadChunk/uploadCommit/uploadAbort); ctxVersion stays 1 (additive). The
    # SAME methods are mirrored in the file-manager fmFile() fallback so its I/O is
    # identical mods on or off, and the transfer + download rewrites drive them.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for src, label in ((loader, "loader ctx.file"), (fm, "fmFile fallback")):
        for sym in ("readChunk: function", "uploadBegin: function",
                    "uploadChunk: function", "uploadCommit: function",
                    "uploadAbort: function"):
            assert sym in src, f"{label} missing #108 method: {sym!r}"
        for route in ("'/file/read_chunk'", "'/file/upload_begin'",
                      "'/file/upload_chunk'", "'/file/upload_commit'",
                      "'/file/upload_abort'"):
            assert route in src, f"{label} does not wrap route {route!r}"
    # ctxVersion unchanged (additive capability).
    assert "ctxVersion: 1" in loader
    # The new routes reach the served page.
    for route in ("'/file/read_chunk'", "'/file/upload_begin'",
                  "'/file/upload_commit'"):
        assert route in INDEX_HTML, \
            f"#108 route missing from served page: {route!r}"
    # The transfer + download rewrites actually DRIVE the session (not the old
    # whole-file read/upload): the chunked calls appear in the mod, and the in-app
    # download opens the File System Access save picker.
    for sym in ("fmFile().uploadBegin(", "fmFile().readChunk(",
                "fmFile().uploadChunk(", "fmFile().uploadCommit(",
                "fmFile().uploadAbort(", "showSaveFilePicker"):
        assert sym in fm, f"file manager missing #108 wiring: {sym!r}"
    # The dead download >5 MiB special-casing is gone from the byte path (the
    # OS-drop whole-file upload keeps its cap, out of scope for #108).
    assert "too large to download" not in fm, \
        "dead download >5 MiB copy remains on a #108 byte path"
    # The mod still routes ALL I/O through the capability — no raw fetch snuck in
    # with the streaming rewrite.
    assert "fileApiPost(" not in fm and "hostFetch(" not in fm


def test_checksum_verified_move_present():
    # #110: ctx.file gains hash() and threads expected_sha256 into uploadCommit;
    # the file-manager's cross-host MOVE hashes the source and gates the source-
    # delete on a VERIFIED commit. ctxVersion stays 1 (additive). The capability is
    # mirrored in the fmFile() fallback so I/O is identical mods on or off.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for src, label in ((loader, "loader ctx.file"), (fm, "fmFile fallback")):
        assert "hash: function" in src, f"{label} missing hash() method"
        assert "'/file/hash'" in src, f"{label} does not wrap /file/hash"
        # expected_sha256 is conditionally threaded into the commit body (not the
        # old bare {upload_id} literal), matching read/write's field style.
        assert "expected_sha256" in src, \
            f"{label} does not thread expected_sha256 into uploadCommit"
    # ctxVersion unchanged (additive capability).
    assert "ctxVersion: 1" in loader
    # The new route + method reach the served page.
    assert "'/file/hash'" in INDEX_HTML and "hash: function" in INDEX_HTML, \
        "#110 /file/hash missing from served page"
    # The MOVE actually DRIVES the verification: hash the source, and a distinct
    # checksum-mismatch outcome keeps the source (never a silent bad delete).
    assert "fmFile().hash(" in fm, "move does not hash the source"
    assert "checksum_mismatch" in fm, "move does not handle a checksum_mismatch"
    assert "checksum mismatch" in fm, "move missing a checksum-mismatch notice"
    # The old size-only move check is gone (the SHA-256 match supersedes it).
    assert "the source changed" not in fm, \
        "dead size-only move check remains — superseded by the SHA-256 gate"
    # The mod still routes ALL I/O through the capability — no raw fetch snuck in.
    assert "fileApiPost(" not in fm and "hostFetch(" not in fm


def test_transfer_progress_window_present():
    # #109: cross-host transfer + in-app download show a Win9x-style modal
    # progress window with a byte-accurate bar + a working Cancel. Core adds ONE
    # reusable helper (openProgressDialog) that owns its AbortController; the
    # file-manager threads that handle's byte-progress + signal into the #108
    # chunk loops. Behavior is exercised live (Playwright); these sentinels lock
    # the wiring. No server test is needed — Cancel reuses the #108
    # /file/upload_abort path, whose partial-dest removal + idempotency is covered
    # by tests/test_file_api.py::test_upload_abort_removes_temp_and_is_idempotent.
    dlg = (BROKER_DIR / "69_js_dialog.js").read_text(encoding="utf-8")
    assert "function openProgressDialog" in dlg
    assert "function openProgressDialog" in INDEX_HTML
    # The helper owns the AbortController that Cancel aborts + the loop reads.
    assert "new AbortController" in dlg

    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    # Opened at BOTH call sites — cross-host transfer (doTransfer) + download
    # (downloadRow).
    assert fm.count("openProgressDialog(") >= 2, \
        "openProgressDialog must be wired at both the transfer and download sites"
    # The handle's byte progress + AbortSignal are threaded into transferTo's
    # existing chunk-loop opts; the download drives update/close directly.
    assert "onProgress:" in fm and "signal:" in fm
    assert "progress.update" in fm
    assert "progress.close(" in fm
    # Cancel's server-side partial-dest teardown still rides the #108 abort path.
    assert "fmFile().uploadAbort(" in fm
    # The mod still routes ALL I/O through the capability — no raw fetch snuck in
    # with the progress/cancel wiring.
    assert "fileApiPost(" not in fm and "hostFetch(" not in fm

    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    assert ".app-dialog-progress" in css
    assert ".app-dialog-progress-fill" in css
    assert ".app-dialog-progress" in INDEX_HTML


def test_filemanager_richer_menu_present():
    # #72: the file manager grows a full right-click menu set + clipboard + drag.
    # These symbol sentinels lock the wiring (the Playwright flow exercises the
    # behavior). The FM routes every confirm/prompt through the styled dialog
    # component — NO native confirm()/prompt() survives in the mod.
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for sym in ("const doTransfer", "const buildRowMenu", "const buildEmptyMenu",
                "const setClipboard", "const pasteInto", "const validateName",
                "const newFolder", "const renameRow", "const deleteRow",
                "const downloadRow", "const zipRow", "const unzipRow",
                "const showProperties", "const makeDraggable",
                "win.fmClipboard"):
        assert sym in fm, f"file manager missing #72 symbol: {sym!r}"
    # Uses the styled dialog component, not native modals. Properties moved from
    # the read-only openInfoModal to the editable openDialog primitive (#96), so
    # the mod now calls openDialog directly (openInfoModal stays defined in core).
    for sym in ("openConfirmDialog(", "openTextPrompt(", "openDialog("):
        assert sym in fm, f"file manager should use styled dialog: {sym!r}"
    import re
    assert not re.search(r"(?<![A-Za-z])confirm\(", fm), \
        "native confirm() must be gone from the file manager (use openConfirmDialog)"
    assert not re.search(r"(?<![A-Za-z])prompt\(", fm), \
        "native prompt() must be gone from the file manager (use openTextPrompt)"
    # The drag payload now carries the entry type (cross-host dir refusal).
    assert "type: ent.type" in fm
    # And the menu wiring reaches the served page.
    assert "buildRowMenu" in INDEX_HTML and "buildEmptyMenu" in INDEX_HTML


def test_properties_dialog_editable_present():
    # #96: Properties is editable + platform-aware. The dialog Saves via the
    # capability wrapper (never a raw fetch) and carries both the Windows
    # 'Attributes' block and the POSIX 'Permissions' grid. Lock the sentinels in
    # the mod AND in the served page (a one-sided drift would otherwise slip by).
    fm = (BROKER_DIR / "mods" / "file-manager" / "file-manager.js").read_text(
        encoding="utf-8")
    for sym in ("fmFile().setattr(", "'Attributes'", "'Permissions'"):
        assert sym in fm, f"editable Properties dialog missing {sym!r}"
        assert sym in INDEX_HTML, \
            f"editable Properties sentinel missing from served page: {sym!r}"


def test_file_capability_trust_doc_present():
    # The trust-tier doc ships in-code WITH the capability: ctx.file is operator-
    # granted REVIEW HYGIENE, not enforcement (a same-origin mod can already POST
    # /file/* directly), and there is NO editor_root confinement.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "REVIEW HYGIENE" in loader
    assert "permission boundary" in loader
    assert "POST to /file/* directly" in loader
    assert "editor_root confinement" in loader


# --------------------------------------------------------------------------- #
# task-manager mod (#85 / S12)
# --------------------------------------------------------------------------- #

def test_taskmanager_symbols_removed_from_core_fragments():
    # The task manager is now a mod (#85/S12): its built-in registration is gone
    # from core 54 and its launcher from core 76 — both moved into
    # mods/task-manager/. The core fragment 72_js_task_manager.js is DELETED.
    assert not (BROKER_DIR / "72_js_task_manager.js").exists()
    assert "72_js_task_manager.js" not in ui._ORDERED
    gone = {
        "54_js_app_windows_store.js": ("appKind: 'task-manager'",
                                       "return openTaskManagerWindow(d)"),
        "76_js_launch_fullscreen.js": ("function launchTaskManager",),
    }
    for name, symbols in gone.items():
        text = (BROKER_DIR / name).read_text(encoding="utf-8")
        for sym in symbols:
            assert sym not in text, f"{sym!r} should be gone from core fragment {name}"
    # The moved builder + launcher are present + reachable in the served page (they
    # ship in mods/task-manager/ as hoisted functions).
    for sym in ("function openTaskManagerWindow", "function launchTaskManager"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"


def test_taskmanager_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "task-manager"
    tm_js = mod_dir / "task-manager.js"
    manifest = mod_dir / "mod.json"
    assert tm_js.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "task-manager"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "task-manager.js"
    assert "mods/task-manager/task-manager.js" in ui._MODS
    src = tm_js.read_text(encoding="utf-8")
    # Registers the task-manager mod + contributes the task-manager window kind
    # through ctx.registerWindowKind.
    assert "registerMod(" in src
    assert "id: 'task-manager'" in src
    assert "ctxVersion: 1" in src
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'task-manager'" in src
    assert "return openTaskManagerWindow(d)" in src
    assert "return launchTaskManager()" in src
    # EPHEMERAL: the kind is registered with NO serialize (never persisted), so
    # there is no `serialize:` key in the spec.
    assert "serialize:" not in src, "task-manager is ephemeral — no serialize key"
    # Session RPC (incl. the DESTRUCTIVE kill / session destroy) rides ctx.session
    # (#85): the mod stashes ctx.session and EVERY /session/* call flows through
    # tmSession() carrying the session's own host id — NO raw inline fetch
    # (hostFetch) and NO surviving inline sessionPost in the mod.
    assert "tmSession.cap = ctx.session;" in src
    assert "tmSession().procs(sess.id, { host: sess.hostId })" in src
    assert "tmSession().kill(sess.id, sess.pid, { host: sess.hostId })" in src
    assert "tmSession().kill(sess.id, pid, { host: sess.hostId })" in src
    assert "hostFetch(" not in src, "the raw inline session fetch must be gone"
    assert "sessionPost(" not in src, "the old inline sessionPost must be gone"
    # Teardown closes any live task-manager window WHILE the kind is still
    # registered (so saveAppWindow early-returns — no junk record persists), then
    # drops the cap. The close-on-unload is registered AFTER registerWindowKind so
    # LIFO teardown runs it BEFORE deleteWindowKind.
    assert "closeWindow(w.id)" in src
    assert "tmSession.cap = null;" in src
    # Ships in the served page, AFTER the help mod and BEFORE the file-manager mod
    # (so the (+) menu lists Task manager right after the core built-ins, ahead of
    # the file-manager / editor / sticky mods).
    assert "id: 'task-manager'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'help'") < INDEX_HTML.index("id: 'task-manager'")
    assert INDEX_HTML.index("id: 'task-manager'") < INDEX_HTML.index("id: 'file-manager'")


# --------------------------------------------------------------------------- #
# ctx.session capability (#85 / S12)
# --------------------------------------------------------------------------- #

def test_session_capability_present():
    # #85 (S12): the ctx.session wrapper over /session/procs + the DESTRUCTIVE
    # /session/kill, plus its host-routing helpers, ride in the served loader. The
    # task-manager mod (and the Playwright acceptance) depend on these. ctxVersion
    # stays 1 (additive capability).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # The capability object + its two methods, on the per-mod ctx.
    for sym in ("session: {",
                "procs: function (id, opts)",
                "kill: function (id, pid, opts)"):
        assert sym in loader, f"missing ctx.session method: {sym!r}"
    # Each method targets the matching /session/* route, wrapped here.
    for route in ("'/session/procs'", "'/session/kill'"):
        assert route in loader, f"ctx.session does not wrap route {route!r}"
    # The host-routing helpers: fail-closed resolution + the task-manager's OWN
    # synthetic no_host error (NOT ctx.file's host_not_found), so rendered errors
    # stay byte-identical to the old inline sessionPost.
    for sym in ("function _modSessionHost", "function _modSessionApi",
                "error: 'no_host'"):
        assert sym in loader, f"missing ctx.session host-routing symbol: {sym!r}"
    # Routing reuses the EXISTING core host helpers (no parallel host logic).
    for sym in ("hostById(hostId)", "return localHost();",
                "hostFetch(host, route"):
        assert sym in loader, f"ctx.session must reuse core host helper: {sym!r}"
    # The {status,json} contract PRESERVES the HTTP status (so a 409 + session_gone
    # stays a 409 — the session-destroy success path) and never rejects.
    assert "{ status: r.status, json: j }" in loader
    # ctxVersion is unchanged — ctx.session is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modSessionApi", "session: {", "error: 'no_host'"):
        assert sym in INDEX_HTML, f"ctx.session missing from served page: {sym!r}"


def test_session_capability_trust_doc_present():
    # The trust-tier doc ships in-code WITH the capability: ctx.session is operator-
    # granted REVIEW HYGIENE for a HIGH-trust (destructive) RPC, not enforcement (a
    # same-origin mod can already POST /session/* directly). The 409 session_gone
    # destroy-success path is documented in-code.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "REVIEW HYGIENE" in loader
    assert "POST to /session/*" in loader
    assert "session_gone" in loader


# --------------------------------------------------------------------------- #
# git status mod (#116 / S14)
# --------------------------------------------------------------------------- #

def test_git_symbols_removed_from_core_fragments():
    # The per-terminal git status widget is now a mod (#116/S14): its inline JS
    # left 67_js_window_lifecycle.js and its CSS left 10_css_root.css — both moved
    # into mods/git/. Only the ctx.windows.onTerminalCreate emit hook stays in core.
    core_js = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    for sym in ("btn-git", "git-popover", "git-label", "refreshGit", "renderGit",
                "gitPost", "/session/git", "gitStatus", "gitTimer"):
        assert sym not in core_js, \
            f"{sym!r} should be gone from 67_js_window_lifecycle.js"
    core_css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    for sym in ("btn-git", "git-label", "git-popover", "git-pop-"):
        assert sym not in core_css, f"{sym!r} should be gone from 10_css_root.css"
    # The shared title-bar anchor STAYS (the color-swatch / MCP popovers need it),
    # but its comment no longer claims to be git-only.
    assert "position: relative;" in core_css
    assert "anchor for the git status popover" not in core_css
    # The per-terminal-window emit hook the mod subscribes to DOES remain in core.
    assert "function registerTerminalCreate" in core_js
    assert "onTerminalCreate" in core_js


def test_git_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "git"
    git_js = mod_dir / "git.js"
    git_css = mod_dir / "git.css"
    manifest = mod_dir / "mod.json"
    assert git_js.is_file() and git_css.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "git"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "git.js"
    assert meta["styles"] == ["git.css"]
    assert "mods/git/git.js" in ui._MODS
    src = git_js.read_text(encoding="utf-8")
    # Registers the git mod, default-OFF, with the reviewed tiers.
    assert "registerMod(" in src
    assert "id: 'git'" in src
    assert "ctxVersion: 1" in src
    assert "defaultEnabled: false" in src
    assert "tiers: ['session', 'window']" in src
    # Rides the per-terminal-window hook + the session git capability (#116),
    # feature-detected — NOT a raw inline fetch (no hostFetch in the mod).
    assert "if (!ctx.windows) return;" in src
    assert "ctx.windows.onTerminalCreate(" in src
    assert "ctx.session.git(" in src
    assert "hostFetch(" not in src, "the raw inline git fetch must be gone from the mod"
    # Per-window teardown covers BOTH a window close (onDispose) and a mod disable
    # (ctx.onUnload drains the disposer set) — no stray interval / orphan DOM.
    assert "info.onDispose(" in src
    assert "ctx.onUnload(" in src
    assert "clearInterval(" in src
    # Ships in the served page, AFTER the aistatus mod (appended last in _MODS).
    assert "id: 'git'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'aistatus'") < INDEX_HTML.index("id: 'git'")
    # The moved CSS rides the served page via the mod-css splice.
    assert ".git-popover" in INDEX_HTML


def test_windows_capability_and_session_git_present():
    # #116 (S14): the additive ctx.windows per-terminal-window hook + ctx.session.git
    # (wrapping the /session/git route) ride in the served loader, and the core emit
    # lives in 67_js_window_lifecycle.js. The git mod + Playwright acceptance depend
    # on these. ctxVersion stays 1 (both additive).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in ("windows: {", "onTerminalCreate: function (cb)",
                "registerTerminalCreate(cb)",
                "git: function (id, opts)", "'/session/git'"):
        assert sym in loader, f"missing #116 loader capability symbol: {sym!r}"
    assert "ctxVersion: 1" in loader
    # The core hook + its create-time emit ride core fragment 67.
    core = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    for sym in ("function registerTerminalCreate", "function _emitTerminalCreate",
                "const termCreateCbs", "addTitleBarItem", "onDispose"):
        assert sym in core, f"missing #116 core hook symbol: {sym!r}"
    # And it all reaches the served page.
    for sym in ("onTerminalCreate", "git: function (id, opts)", "'/session/git'"):
        assert sym in INDEX_HTML, f"#116 capability missing from served page: {sym!r}"


def test_default_enabled_capability_present():
    # #106/#116: the loader's defaultEnabled capability the git (and aistatus #112)
    # default-off mods ride, and which #106 inherits. registerMod records the
    # declared default; the enable state resolves default XOR override, so the
    # persisted set means "ids toggled AWAY from their default" — key unchanged
    # (zero migration).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in ("defaultEnabled: (decl.defaultEnabled !== false)",
                "function _modDefault",
                "_modsDisabled().has(id) ? !def : def",
                "if (on === _modDefault(id)) set.delete(id); else set.add(id);",
                "TOGGLED AWAY",
                "'webterm:mods:disabled'"):
        assert sym in loader, f"missing defaultEnabled loader symbol: {sym!r}"
    assert "function _modDefault" in INDEX_HTML
    # #106: the test API surfaces the declared default so an acceptance can assert
    # an opt-in mod ships defaultEnabled:false via window.__mods.__test.registered().
    assert "defaultEnabled: (m.defaultEnabled !== false)" in loader


# --------------------------------------------------------------------------- #
# `requires` mod-dependency primitive (#121 / S15)
# --------------------------------------------------------------------------- #

def test_requires_capability_present():
    # #121: the loader's `requires` mod-dependency plumbing — registerMod
    # normalization, the initMod precondition guard (a new structured `requires`
    # reason, never a throw), the setModEnabled enable/disable cascades, the
    # Mods-pane read-only `blocked` status, and the test-API surface. Static-checked
    # here; runtime is verified manually via Playwright-MCP (no JS runner exists).
    # Ordering correctness is guarded separately by
    # test_requires_declared_before_dependency_in_mods_list.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in (
        # registerMod normalizes requires -> [] when omitted (every existing mod).
        "requires: Array.isArray(decl.requires)",
        # initMod precondition: a mod whose required deps are inactive is BLOCKED
        # (structured result, never throws) — no slot claimed, no partial init.
        "reason: 'requires'",
        "return !window.__mods.active.has(dep);",
        # #163: and a mod IN a dependency cycle gets its own structured reason,
        # so it never reads as an ordinary dependency block.
        "reason: 'cycle'",
        # setModEnabled cascades: a forward enable pass + a reverse disable pass.
        "regs.indexOf(decl) + 1",       # enable: init later deps-satisfied mods
        "const doomed = new Set([id]);",  # disable: transitive-dependent closure
        # the test API surfaces declared deps for the Playwright acceptance.
        "requires: (m.requires || []).slice()",
    ):
        assert sym in loader, f"missing #121 requires loader symbol: {sym!r}"
    # #163: the ordering invariant those cascades assume is no longer established
    # by ui._MODS's position rule alone — it is re-established at RUNTIME by the
    # topological sort, and the `blocked` classification moved into the union
    # status model (a cycle row / a 404'd package has no registered[] entry to
    # classify from). Both live in 86b_js_mod_packages.js.
    pkgs = _packages_src()
    for sym in (
        "function _topoSortRegistered(",
        "function _modKahn(",
        "function _modCycleMembers(",
        # the Mods pane reflects a dependency block READ-ONLY (needs: <ids>).
        "state = 'blocked'",
        "'needs: '",
    ):
        assert sym in pkgs, f"missing #163 requires symbol: {sym!r}"
    # ctxVersion is unchanged — requires is additive plumbing.
    assert "ctxVersion: 1" in loader
    # And the key symbols reach the served page.
    for sym in ("requires: Array.isArray(decl.requires)",
                "reason: 'requires'",
                "reason: 'cycle'",
                "state = 'blocked'",
                "function _topoSortRegistered(",
                "requires: (m.requires || []).slice()"):
        assert sym in INDEX_HTML, \
            f"#121 requires symbol missing from served page: {sym!r}"


def test_requires_declared_before_dependency_in_mods_list():
    # For every in-repo mod that declares requires:[ids] in its registerMod,
    # assert each listed id (i) is a KNOWN mod and (ii) is registered STRICTLY
    # EARLIER in ui._MODS, which makes cycles, self-require and missing
    # dependencies unrepresentable across the SHIPPED set.
    #
    # #163 changed what this guard MEANS. It used to stand in for a runtime
    # topological sort: a runtime-installed set has no _MODS list, so the loader
    # now sorts (Kahn) and splits cycles from blocked-by-cycle (Tarjan) at boot
    # — see test_installed_package_topological_sort_present. The positional rule
    # survives as a STYLE rule on the shipped set: it is still true, still cheap,
    # and it keeps the shipped half of the graph readable top-to-bottom. What it
    # is no longer is the only thing standing between us and a cycle.
    import re
    # Map every mod id -> its load index. The registrant is the mods/<id>/<id>.js
    # entry (a helper-only sibling like editor/codemirror.js registers nothing).
    id_to_index = {}
    for i, entry in enumerate(ui._MODS):
        p = PurePosixPath(entry)
        if p.stem == p.parent.name:
            id_to_index[p.stem] = i
    for mod_id, idx in id_to_index.items():
        src = (BROKER_DIR / "mods" / mod_id / f"{mod_id}.js").read_text(
            encoding="utf-8")
        m = re.search(r"id:\s*'%s'.*?requires:\s*\[([^\]]*)\]" % re.escape(mod_id),
                      src, re.S)
        if not m:
            continue  # no requires: declared -> nothing to order-check
        for dep in re.findall(r"'([a-z0-9-]+)'", m.group(1)):
            # #172/#163: a SHIPPED mod may never depend on an "x-" (runtime-
            # installed) id. It keeps shipped->installed edges unrepresentable,
            # which is what lets /info emit every shipped row first while the
            # installed half is sorted topologically after it -- and a shipped
            # mod that needed an installed one would be broken on any broker
            # that had not installed it. Asserted against the JS here and
            # against mod.json in test_no_shipped_mod_requires_an_installed_mod_id.
            assert not dep.startswith("x-"), (
                f"mod {mod_id!r} requires {dep!r}, which is in the "
                f"runtime-installed namespace")
            assert dep in id_to_index, (
                f"mod {mod_id!r} requires unknown mod id {dep!r} "
                f"(not a registrant in ui._MODS)")
            assert id_to_index[dep] < idx, (
                f"mod {mod_id!r} requires {dep!r} but it is not registered earlier "
                f"in ui._MODS (dep at index {id_to_index[dep]}, dependent at {idx})")


# --------------------------------------------------------------------------- #
# runtime-installed mod packages -- the loader half (#163 / S4)
# --------------------------------------------------------------------------- #

def _order_in(body, *needles):
    """Every needle appears, in this order, in `body`. Returns the offsets so a
    failure can name what actually came first."""
    at = []
    for n in needles:
        assert n in body, f"missing: {n!r}"
        at.append(body.index(n))
    assert at == sorted(at), (
        "out of order: " + ", ".join(f"{n!r}@{i}" for n, i in zip(needles, at)))
    return at


def test_loadmods_sequence_puts_pin_resolution_after_the_package_load():
    # #163: the boot resequencing, which is LOAD-BEARING and not cosmetic.
    #
    #   1 await localInfo()            stash policyRaw + catalog, resolve NOTHING
    #   2 master gate                  return BEFORE a single script is fetched
    #   3 await _loadInstalledPackages the installed registerMod calls land here
    #   4 _topoSortRegistered()        re-establish dependency order
    #   5 _resolvePins(policyRaw)      MUST follow 3-4: _resolvePins drops any
    #                                  pin whose id is not already in
    #                                  `registered`, so resolving at the old
    #                                  position would silently discard every pin
    #                                  naming an installed mod
    #   6 _mountModsManagerPane()      MUST follow 3: _modRegisterPane calls
    #                                  spec.render() exactly ONCE, so mounting
    #                                  first would freeze a stale row set
    #   7 prune -> the forward boot loop
    body = _loader_fn("async function loadMods(")
    _order_in(
        body,
        "if (_nomodsRequested()) {",              # 0 - before everything
        "const info = await localInfo();",        # 1
        "window.__mods.policyRaw = (info && info.mod_policy) || null;",
        "window.__mods.catalog = (info && Array.isArray(info.mods))",
        "if (!enabled) {",                        # 2 - the absolute master gate
        "await _loadInstalledPackages(window.__mods.catalog);",   # 3
        "_topoSortRegistered();",                                  # 4
        "window.__mods.policy = _resolvePins(window.__mods.policyRaw);",  # 5
        "_mountModsManagerPane();",                                # 6
        "const disabled = _modsDisabled();",                       # 7
        "initMod(decl);",
        "window.__mods.bootComplete = true;",
    )
    # The master-off return really is before the fetch, not merely before the
    # boot loop: nothing is requested from a broker that disables mods.
    gate = body.index("if (!enabled) {")
    assert body.index("await _loadInstalledPackages(") > gate
    # The pins are STASHED at step 1, never resolved there. (Comments stripped:
    # the block deliberately EXPLAINS _resolvePins, it just must not call it.)
    stash = "\n".join(
        line for line in body[body.index("const info = await localInfo();"):
                              body.index("await _loadInstalledPackages(")].splitlines()
        if not line.strip().startswith("//"))
    assert "_resolvePins" not in stash, \
        "resolving pins before the packages load drops every installed-mod pin"
    # policyResolved gates notifyModsHostAuth, so it must not go up before the
    # policy it advertises exists.
    assert body.rindex("window.__mods.policyResolved = true;") \
        > body.index("window.__mods.policy = _resolvePins("), \
        "policyResolved must follow the pin resolution it advertises"
    # The master-off early return still marks it resolved (unchanged #157
    # behaviour: a mods-off broker has answered, there is nothing to re-ask).
    off = body[gate:body.index("await _loadInstalledPackages(")]
    assert "window.__mods.policyResolved = true;" in off
    # A defect in the installed-package machinery must not cost the SHIPPED mods
    # their init: loadMods is fire-and-forget, so a throw here would skip the
    # boot loop entirely and leave the desktop with no mods at all.
    assert "try {\n                await _loadInstalledPackages(" in body
    assert "try { _mountModsManagerPane(); }" in body


def test_disabled_prune_keeps_catalog_ids_not_just_registered_ones():
    # #163: an installed mod whose script 404s / times out / fails SRI is NOT in
    # `registered`, so the old prune would drop its id from
    # webterm:mods:disabled and the mod would come up ENABLED on the next page
    # load -- silently discarding the user's explicit "off" for exactly the mod
    # that most needs it off.
    body = _loader_fn("async function loadMods(")
    assert "for (const row of window.__mods.catalog) {" in body
    assert "if (row && typeof row.id === 'string' && row.id) known.add(row.id);" \
        in body
    prune = body[body.index("const known = new Set("):]
    assert prune.index("known.add(row.id)") < prune.index("if (!known.has(id))"), \
        "the catalog ids must join the keep-set BEFORE the prune walks it"
    # ...and the whole pass is SKIPPED unless the boot /info answered. A 401 (no
    # token stored yet) gives an EMPTY catalog, and pruning against that would
    # delete every installed mod's choice on exactly the page load that could
    # not see that they exist.
    assert body.index("if (window.__mods.policyAuthoritative) {") \
        < body.index("const known = new Set("), \
        "the prune must not run from a view it knows is incomplete"


def test_installed_package_topological_sort_present():
    # #163 / design §6. Ordering used to be positional in ui._MODS; a runtime-
    # installed set has no such list, so the invariant moves from
    # test-established to runtime-established.
    pkgs = _packages_src()
    body = _frag_fn(pkgs, "function _topoSortRegistered(")
    # ONE graph over shipped u installed. An edge to an id in NEITHER is dropped
    # from the sort and recorded -- it must not contribute an indegree, or an
    # installed mod requiring a SHIPPED mod would come out marked cyclic.
    assert "const universe = new Set(index.keys());" in body
    assert "universe.add(row.id)" in body
    assert "} else if (!universe.has(dep)) {" in body
    assert "absent.push(dep);" in body
    assert "window.__mods.missingRequires = missing;" in body
    # Kahn's residual is NOT the cycle set (A->B, B->A, C->A leaves {A,B,C} but
    # C is merely blocked BY a cycle), so the residual is SPLIT with Tarjan.
    assert "const order = _modKahn(regs, edges, index);" in body
    assert "const inCycle = _modCycleMembers(residual, edges);" in body
    assert "cycleState[id] = inCycle.has(id) ? 'cycle' : 'blocked-by-cycle';" in body
    kahn = _frag_fn(pkgs, "function _modKahn(")
    assert "if (index.get(ready[i]) < index.get(ready[best])) best = i;" in kahn, \
        "ties must break on the CURRENT index or the shipped order is not preserved"
    scc = _frag_fn(pkgs, "function _modCycleMembers(")
    assert "component.length > 1" in scc and ".indexOf(node) !== -1" in scc, \
        "an SCC of size > 1 OR a self-loop is what makes a node in-cycle"
    assert "const work = [[root, 0]];" in scc, \
        "Tarjan must be iterative so a deep graph cannot blow the stack"
    # Reordered IN PLACE: _bringUp / _takeDown / _applyPolicyLive / _resolvePins
    # all hold the same array and all assume dependency-precedes-dependent.
    assert "regs.length = 0;" in body and "for (const m of sorted) regs.push(m);" in body
    assert "window.__mods.registered = " not in pkgs, \
        "replacing the array would strand every holder of the old one"


def test_installed_packages_load_once_async_and_sri_pinned():
    # #163 / design §5. One request per (id, gen, file), script.async = true (NOT
    # ordered-async -- ordering is irrelevant because the topo sort runs
    # afterwards, and ordered-async lets one slow file head-of-line block every
    # mod), SRI from the catalog, a <link> per style at the same generation URL,
    # and a deadline that PROCEEDS rather than cancelling.
    pkgs = _packages_src()
    body = _frag_fn(pkgs, "function _loadInstalledPackages(")
    assert "const MOD_SCRIPT_TIMEOUT_MS = 5000;" in body
    # Function-LOCAL, never a fragment-level let/const: a hoisted function
    # reading a not-yet-initialized fragment binding throws a TDZ ReferenceError
    # that disables the whole mod, and CI never runs this JS (#169's rule).
    for banned in ("\n        const MOD_SCRIPT_TIMEOUT_MS",
                   "\n        let MOD_SCRIPT_TIMEOUT_MS"):
        assert banned not in pkgs, f"{banned.strip()!r} is a TDZ hazard here"
    # The deadline proceeds; it cannot cancel an in-flight <script>.
    assert "if (!pkg.done) pkg.state = 'timeout';" in body
    assert "PROCEED-ANYWAY deadline, NOT a cancel" in body
    # One bad row must not cost the others their load.
    assert "console.error('[mods] could not start installed package'," in body
    start = _frag_fn(pkgs, "function _startPackage(")
    assert "s.async = true;" in start
    assert "s.async = false" not in pkgs, "ordered-async head-of-line blocks"
    assert "s.dataset.modPackage = pkg.id;" in start
    assert "if (sri) s.integrity = sri;" in start
    assert "if (sri) link.integrity = sri;" in start
    assert "link.rel = 'stylesheet';" in start
    # A row the broker marked broken is NOT fetched at all.
    assert "if (row.error) {" in start
    skip = start[start.index("if (row.error) {"):]
    assert skip.index("return null;") < skip.index("_loadModAsset("), \
        "a cycle row must never reach the network"
    # A package's OWN scripts run in manifest order: the topo sort orders mod
    # DECLARATIONS, not the files of one mod, so a package whose second file
    # reads a global its first defined would work or not by network timing.
    assert "let chain = Promise.resolve(true);" in start
    assert "chain = chain.then(function (okSoFar) {" in start
    assert "return okSoFar && r.ok;" in start, \
        "any failed file must fail the whole package"
    # Deduplicated on KIND + URL, and the URL encodes (id, gen, file) -- the
    # post-login retry re-walks the whole catalog and must not execute anything
    # twice (D1).
    asset = _frag_fn(pkgs, "function _loadModAsset(")
    assert "const key = kind + ' ' + url;" in asset
    assert "if (inflight[key]) return inflight[key];" in asset
    assert "inflight[key] = p;" in asset
    # Never rejects: load -> ok, error (404 / transport / SRI MISMATCH) -> !ok,
    # and a DOM that refuses the insertion -> !ok rather than a rejection.
    assert "resolve({ ok: ok, url: url });" in asset
    assert "reject" not in asset
    assert asset.index("try {") < asset.index("appendChild(el);") \
        < asset.index("} catch (e) {"), \
        "element construction AND insertion must be inside the try"
    # Same-origin, generation-qualified, every segment encoded.
    url = _frag_fn(pkgs, "function _modAssetUrl(")
    assert "return '/mods/' + encodeURIComponent(id) + '/'" in url
    assert "token" not in url, "an asset URL must never carry the token (#144)"


def test_late_registration_is_gated_on_what_this_page_requested():
    # #163: a package that overran the load deadline still executes -- nothing
    # can cancel an in-flight <script>. _lateRegister is the only way in, and it
    # accepts ONLY an id this page asked for, so a script landing minutes later
    # for a package this page never requested cannot bring a mod up.
    loader = _loader_src()
    pkgs = _packages_src()
    reg = _frag_fn(loader, "function registerMod(")
    # Gated on `sorted`, NOT on "the boot loop finished": a mod that registers
    # another from its own init() lands DURING the loop, and a declaration
    # appended after the one sort but never sorted itself would permanently
    # break the dependency-precedes-dependent invariant every cascade assumes.
    assert "if (window.__mods.sorted) _lateRegister(entry);" in reg
    boot = _loader_fn("async function loadMods(")
    assert boot.index("window.__mods.sorted = true;") \
        < boot.index("for (const decl of window.__mods.registered.slice()) {")
    # ...and the boot loop must then tolerate a mod the nested path already
    # brought up, or initMod reports a ModConflictError that is not one.
    assert "if (window.__mods.active.has(decl.id)) continue;" in boot
    body = _frag_fn(pkgs, "function _lateRegister(")
    assert "if (!_modBag('requested')[decl.id]) {" in body
    assert "reg.splice(i, 1);" in body, \
        "a refused late registration must be un-registered, not merely ignored"
    assert "reason: 'not-requested'" in body
    # On accept: re-sort, THEN re-resolve the pins (a pin naming this mod is
    # dropped by _resolvePins until the mod is in `registered`), then repaint
    # the pane, then bring it up.
    _order_in(body,
              "_topoSortRegistered();",
              "window.__mods.policy = _resolvePins(window.__mods.policyRaw);",
              "_renderManagerRows();",
              "if (_modPinSig(window.__mods.policy) !== before) {",
              "_applyPolicyLive();",
              "if (isModEnabled(decl.id)) {",
              "_bringUp(decl);")
    # If the pin map MOVED, the whole policy is reconciled rather than just this
    # mod: a pin naming it pins its `requires` on too, and those dependencies
    # are registered EARLIER -- which _bringUp cannot reach, because it inits
    # `decl` and then walks FORWARD only. For a deadline straggler no later
    # _applyPolicyLive ever runs, so without this both stay inactive forever.
    assert "function _modPinSig(" in pkgs
    # The master gate stays absolute: a late arrival cannot init past it.
    assert body.index("if (window.__mods.masterEnabled === false) {") \
        < body.index("_applyPolicyLive();")
    # Its second caller: the post-login path loads the packages a 401'd boot
    # never saw -- otherwise typing your password leaves every installed mod
    # missing until a reload (the hole #157 closed for the pins).
    auth = _frag_fn(loader, "async function notifyModsHostAuth(")
    _order_in(auth,
              "await _loadInstalledPackages(window.__mods.catalog);",
              "_topoSortRegistered();",
              "window.__mods.policy = _resolvePins(window.__mods.policyRaw);",
              "_applyPolicyLive();")


def test_registration_is_bound_to_its_package_by_currentScript():
    # #163: a declaration whose id is not the executing package's id is refused
    # with a `wrong-id` row. A CORRECTNESS CONVENTION, NOT A BOUNDARY -- and the
    # source has to say so, because fork-trust means the code can bypass it.
    loader = _loader_src()
    reg = _frag_fn(loader, "function registerMod(")
    assert "const pkgId = _currentPackageId();" in reg
    assert "if (pkgId && pkgId !== id) {" in reg
    assert "pkg.state = 'wrong-id'; pkg.wrongId = id;" in reg
    assert "CORRECTNESS CONVENTION, NOT A BOUNDARY" in reg
    # A shipped mod runs inside the ONE inline <script>, which carries no
    # package stamp -- so the check cannot fire for it.
    cur = _frag_fn(_packages_src(), "function _currentPackageId(")
    assert "el.dataset.modPackage || null" in cur
    assert "document.currentScript" in cur


def test_mod_status_model_is_a_union_not_a_registered_walk():
    # #163 / design §5. A cycle row, a 404'd script and an SRI mismatch never
    # call registerMod, so they have NO entry in registered[] -- a pane driven
    # off that array simply cannot show them. The model is the union of catalog
    # packages and registrations, joined on id.
    pkgs = _packages_src()
    rows = _frag_fn(pkgs, "function _modStatusRows(")
    assert "for (const row of cat) {" in rows and \
        "for (const m of window.__mods.registered) {" in rows, \
        "the row set must be a UNION of both sides"
    body = _frag_fn(pkgs, "function _modStatusRow(")
    for state in ("'active'", "'off'", "'blocked'", "'cycle'",
                  "'blocked-by-cycle'", "'failed'", "'fetch-failed'",
                  "'timeout'", "'no-register'", "'wrong-id'"):
        assert f"state = {state};" in body, f"missing status: {state}"
    # `blocked` distinguishes a dep that IS here but off from one that never
    # loaded, and from one this broker does not have at all.
    assert "if (_modIsRegistered(dep)) return dep;" in body
    assert "' (not installed)'" in body and "' (not loaded)'" in body
    # Provenance on every row, and the two states that must not offer a toggle.
    assert "source: source," in body
    assert "toggleable: !!decl && pin === null," in body
    # The broker's own verdict is honoured but the client's wins (the client
    # sees the whole shipped u installed graph; the broker only sorts installed).
    assert "catRow.error === 'requires_cycle'" in body
    assert "catRow.error === 'blocked_by_cycle'" in body
    # S5 renders this; it must reach the served page and the test API.
    assert "function _modStatusRows(" in INDEX_HTML
    assert "statusRows: function () { return _modStatusRows(); }," in _loader_src()


def test_a_partly_loaded_package_is_never_reported_as_merely_active():
    # #163 (adversarial review): a package with SEVERAL scripts can have one
    # register while another 404s or fails SRI. The mod is live, but the package
    # is incomplete -- and a row that read `active` would hide the asset failure
    # completely. So the package check is made BEFORE the "is it registered?"
    # branch, and the label says which case it is.
    body = _frag_fn(_packages_src(), "function _modStatusRow(")
    assert body.index("pkg.state === 'fetch-failed'") < body.index("} else if (!decl) {")
    assert "label = decl ? 'fetch failed (partly loaded)' : 'fetch failed';" in body


def test_installed_css_parity_with_shipped_mod_css_is_stated():
    # #163 (adversarial review): an installed mod's stylesheet is injected for
    # every fetchable package, before any enable/pin check, and no teardown can
    # remove it. That is not an oversight -- it is exactly what ui.py already
    # does for SHIPPED mod CSS (spliced at assembly time, "present but inert",
    # independent of the runtime gate), and the parity has to be written down or
    # the next reader will "fix" one of the two.
    pkgs = _packages_src()
    doc = pkgs[:pkgs.index("function _loadInstalledPackages(")]
    assert "present but inert" in doc
    assert "STYLES are injected but NOT awaited" in doc
    # ...and they really are outside the awaited set: only scripts feed `chain`.
    start = _frag_fn(pkgs, "function _startPackage(")
    assert start.index("for (const name of pkg.styles) {") \
        < start.index("let chain = Promise.resolve(true);")
    assert "chain" not in start[start.index("for (const name of pkg.styles) {"):
                                start.index("let chain = Promise.resolve(true);")]
    # ui.py's own statement of the same rule, so the two cannot silently diverge.
    ui_src = (BROKER_DIR / "ui.py").read_text(encoding="utf-8")
    assert "present-but-inert" in ui_src


def test_installed_mods_are_default_off_whatever_they_declare():
    # #163 / design §2: installing a mod on one broker must not silently switch
    # it on for every browser that loads that broker's page. The broker reports
    # default_enabled false for every installed row; the loader must take the
    # default from the CATALOG for an installed mod, because a package is free
    # to declare `defaultEnabled: true` -- or omit it, which means true -- in its
    # own registerMod call.
    body = _loader_fn("function _modDefault(")
    _order_in(body,
              "const row = _modCatalogRow(id);",
              "if (row && row.source === 'installed') return row.default_enabled === true;",
              "return rec ? rec.defaultEnabled !== false : true;")


def test_nomods_escape_hatch_present():
    # #163: the answer to "an installed mod bricked the desktop and the Control
    # Panel is unreachable". Read-only -- never written to localStorage or
    # /state, so it cannot become sticky.
    pkgs = _packages_src()
    body = _frag_fn(pkgs, "function _nomodsRequested(")
    assert ".get('nomods') === '1'" in body
    assert "localStorage" not in body and "savePrefs" not in body
    boot = _loader_fn("async function loadMods(")
    assert boot.index("if (_nomodsRequested()) {") < boot.index("await localInfo()"), \
        "?nomods=1 must return before the /info fetch, not merely before the boot loop"
    assert "return;" in boot[boot.index("if (_nomodsRequested()) {"):]
    assert "function _nomodsRequested(" in INDEX_HTML
    # ...and the post-login path must honour it too, or logging in would fetch
    # every installed package on the very page that asked for none.
    auth = _loader_fn("async function notifyModsHostAuth(")
    assert auth.index("if (window.__mods.masterEnabled === false) return;") \
        < auth.index("await _loadInstalledPackages(")


def test_installed_mods_are_not_in_the_bundle():
    # #163's central packaging property: an installed mod never enters
    # INDEX_HTML. That is what keeps the CSP hash, the one-inline-<script>
    # assertion, the byte-identity tests, the _MODS drift guard and the per-
    # fragment line caps all working unmodified -- "stale INDEX_HTML" is a
    # non-problem by construction, because the page does not change when a mod
    # is installed.
    # Still exactly ONE inline <script> (inline_script_hash raises otherwise).
    # Strip that block before looking for tags: the loader's own comments
    # legitimately quote the asset-URL shape.
    ui.inline_script_hash(INDEX_HTML)
    import re as _re
    markup = _re.sub(r"<script>.*?</script>", "", INDEX_HTML, flags=_re.S)
    assert 'src="/mods/' not in markup
    assert 'href="/mods/' not in markup
    # ui.mod_catalog() stays SHIPPED-ONLY: it is derived from _MODS, so it can
    # never advertise a mod the page does not carry. app.py concatenates the
    # installed half from modinstall.catalog() at request time.
    assert all(row["source"] == "shipped" for row in ui.mod_catalog())
    # The asset route is the only way installed bytes reach the page, and the
    # loader builds those URLs itself -- there is no assembly-time splice.
    assert "'/mods/' + encodeURIComponent(id)" in INDEX_HTML


# --------------------------------------------------------------------------- #
# clipboard mod + ctx.clipboard observer seam (#106)
# --------------------------------------------------------------------------- #

def test_clipboard_capability_present():
    # #106: the core clipboard observer seam the clipboard mod rides. The notify
    # registry + the copy-OUT / paste-IN emit points live in core (63/67); the
    # loader exposes ctx.clipboard.observe (additive — ctxVersion stays 1). Capture
    # only happens while an observer is registered, so with the (default-off) mod
    # disabled nothing is recorded.
    clip = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    # The registry + notify + subscribe primitives, and the copy-OUT emit inside
    # copyTextToClipboard (one call covers both write branches).
    for sym in ("const _clipboardObservers = new Set()",
                "function _notifyClipboard",
                "function addClipboardObserver",
                "_notifyClipboard('out', text);"):
        assert sym in clip, f"missing #106 clipboard seam symbol in 63: {sym!r}"
    # The paste-IN seams ride 67: the inline notify on the right-click paste path
    # AND a capture-phase 'paste' listener (true) that also works in a non-secure
    # context. Both feed _notifyClipboard('in', ...).
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "_notifyClipboard('in', text);" in life
    assert "addEventListener('paste', onClipPaste, true)" in life
    assert "_notifyClipboard('in', t);" in life
    # The loader capability + its auto-teardown helper (observer removed on the
    # mod's unload, so capturing stops the moment the mod is disabled).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in ("clipboard: {", "observe: function (fn)",
                "function _modClipboardObserve", "addClipboardObserver(fn)"):
        assert sym in loader, f"missing #106 ctx.clipboard loader symbol: {sym!r}"
    assert "ctxVersion: 1" in loader   # additive capability
    # And it all reaches the served page.
    for sym in ("function _notifyClipboard", "function addClipboardObserver",
                "clipboard: {", "function _modClipboardObserve",
                "_notifyClipboard('in',"):
        assert sym in INDEX_HTML, f"#106 clipboard seam missing from served page: {sym!r}"


def test_right_click_paste_routes_through_xterm_paste():
    # #138: the right-click seamless paste must go through xterm's paste()
    # (CRLF/LF -> CR + ESC[200~ bracketing iff the app enabled DECSET 2004,
    # exiting via onData -> sendChunked('input', ...)), NEVER raw ws 'paste'
    # frames that bypass xterm — those submit a multiline block at the first
    # newline instead of inserting it whole.
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "term.paste(text);" in life
    assert "sendChunked('paste'" not in life
    assert "term.paste(text);" in INDEX_HTML
    assert "sendChunked('paste'" not in INDEX_HTML


def test_conpty_bracket_gap_wrap_present():
    # #138 live finding: Windows ConPTY never forwards an app's DECSET 2004
    # request, so xterm can't bracket natively while Claude Code runs — every
    # paste path must route through pasteTextToTerm, which hand-brackets
    # exactly when the gap applies (verified agent foreground + xterm mode
    # off) and defers to term.paste() everywhere else.
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "const needsConptyPasteWrap" in life
    assert "const pasteTextToTerm" in life
    assert "const BRACKET_GAP_AGENTS = { claude: true };" in life
    assert "'\\x1b[200~' + safe + '\\x1b[201~'" in life
    # All three text-injection sites ride the wrap: right-click, Ctrl+V
    # takeover inside onClipPaste, and the #137 image-path injection.
    assert life.count("pasteTextToTerm(") >= 3   # the 3 call sites
    assert "const needsConptyPasteWrap" in INDEX_HTML
    assert "const pasteTextToTerm" in INDEX_HTML


def test_mac_option_click_forces_selection():
    # #154 track 2: xterm's escape gesture out of app-owned mouse tracking is
    # shouldForceSelection -> Shift-drag everywhere except macOS, where it also
    # needs macOptionClickForcesSelection. That option defaults FALSE in the
    # vendored bundle, so without this a Mac user in lazygit/btop has no gesture
    # at all to select text. Must be on the Terminal ctor, and must reach the
    # served page (fragments are concatenated at import).
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "macOptionClickForcesSelection: true" in life
    assert "macOptionClickForcesSelection: true" in INDEX_HTML
    # It has to be INSIDE the `new Terminal({...})` options, not a stray
    # assignment: xterm reads it from rawOptions, so placement is the behaviour.
    ctor = life.split("new Terminal({", 1)[1].split("});", 1)[0]
    assert "macOptionClickForcesSelection: true" in ctor
    # The vendored bundle must still honour the option name we are setting.
    xterm = (BROKER_DIR / "vendor" / "xterm.js").read_text(encoding="utf-8")
    assert "macOptionClickForcesSelection" in xterm


def test_image_paste_wired_into_page():
    # #137: clipboard-image paste. Capture helpers live in 63 (secure-context
    # gate, text-wins image read, base64, prompt quoting); the upload/injection
    # seam lives in 67 (pasteImageBlob + the Ctrl+V / right-click / Alt+V
    # branches); and the whole seam must reach the served page.
    clip = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    for sym in ("function canReadClipboardItems",
                "function readClipboardImageBlob",
                "function blobToBase64",
                "function quotePathForPrompt"):
        assert sym in clip, f"missing #137 clipboard helper in 63: {sym!r}"
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    for sym in ("const pasteImageBlob",
                "/file/paste_image",
                "const handleAltVPaste",
                "sendChunked('input', '\\x1bv')"):
        assert sym in life, f"missing #137 image-paste seam in 67: {sym!r}"
    for sym in ("function canReadClipboardItems",
                "function readClipboardImageBlob",
                "/file/paste_image",
                "pasteImageBlob"):
        assert sym in INDEX_HTML, \
            f"#137 image paste missing from served page: {sym!r}"


def test_clipboard_mod_packaged_and_manifest_agrees():
    import json
    mod_dir = BROKER_DIR / "mods" / "clipboard"
    clip_js = mod_dir / "clipboard.js"
    clip_css = mod_dir / "clipboard.css"
    manifest = mod_dir / "mod.json"
    assert clip_js.is_file() and clip_css.is_file() and manifest.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "clipboard"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "clipboard.js"
    assert meta["styles"] == ["clipboard.css"]
    assert "mods/clipboard/clipboard.js" in ui._MODS
    src = clip_js.read_text(encoding="utf-8")
    # Registers the clipboard mod, default-OFF (opt-in — secrets), reviewed tiers.
    assert "registerMod(" in src
    assert "id: 'clipboard'" in src
    assert "ctxVersion: 1" in src
    assert "defaultEnabled: false" in src
    assert "tiers: ['clipboard', 'window', 'taskbar']" in src
    # Rides the additive ctx.clipboard observer seam, feature-detected — NOT a raw
    # monkey-patch of copyTextToClipboard.
    assert "if (!ctx.clipboard) return;" in src
    assert "ctx.clipboard.observe(" in src
    # Contributes a window kind (EPHEMERAL — NO serialize, never persisted).
    assert "ctx.registerWindowKind(" in src
    assert "appKind: 'clipboard'" in src
    assert "return openClipboardWindow(d)" in src
    assert "return launchClipboard()" in src
    # #118: a taskbar tray chip (open-or-focus, same launchClipboard path), styled
    # via the served page's #clipboard-chip rules.
    assert "ctx.taskbar.addStatusItem(" in src
    assert "clipboard-chip" in src
    assert "#clipboard-chip" in INDEX_HTML
    assert "serialize:" not in src, "clipboard is ephemeral — no serialize key"
    # Re-copy on row click, guarded so re-copying doesn't push a duplicate top entry.
    assert "copyTextToClipboard(entry.text)" in src
    assert "_selfCopy" in src
    # Rows are built with textContent only. Exactly two innerHTML uses, neither
    # carrying user data: the clipBody clear (''), and the #119 tray-chip icon
    # (a trusted, hardcoded APP_ICON_SVG string). Row/entry TEXT is never innerHTML.
    assert src.count(".innerHTML") == 2
    assert "clipBody.innerHTML = ''" in src
    assert "chip.innerHTML = appIconSvg('clipboard')" in src
    # Teardown closes any live clipboard window WHILE the kind is still registered
    # (so saveAppWindow early-returns — no junk record), same as the task-manager.
    assert "closeWindow(w.id)" in src
    assert "ctx.onUnload(" in src
    # Ships in the served page, AFTER the git mod (appended last in _MODS).
    assert "id: 'clipboard'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'git'") < INDEX_HTML.index("id: 'clipboard'")
    # The CSS rides the served page via the mod-css splice.
    assert ".app-clip .clip-row" in INDEX_HTML


# --------------------------------------------------------------------------- #
# packaging + enable/permission UI (#86 / S13)
# --------------------------------------------------------------------------- #

# The trust-tier vocabulary the in-repo mods declare. Kept here (not in the
# loader) as the test's source of truth: a typo or a new unreviewed token trips
# this guard. Mirrors the ctx-capability families a mod can use.
_KNOWN_TIERS = {"settings", "taskbar", "file", "session", "window", "storage",
                # "storage" was localStorage-only (ctx.storage) and unused until
                # #124, which also lets it designate the DURABLE server store
                # (ctx.serverStore); "clipboard" (#106) observes the clipboard seam.
                "clipboard"}

# What each shipped mod is reviewed to use, derived from its actual `ctx.` usage
# (see each mod's registerMod). Hardcoded like the other drift sentinels so an
# accidental tier change in a mod surfaces here for re-review.
_EXPECTED_TIERS = {
    "theme": ["settings"],
    "pattern": ["settings"],
    "clock": ["taskbar", "settings"],
    "help": ["taskbar"],   # #101: dropped the synced showHelpButton key; chip only
    "task-manager": ["session", "window"],
    "file-manager": ["file", "window"],
    "editor": ["file", "window"],
    # (#177 retired agent-docs -- ["file", "window"] -- to mods-deprecated/.)
    "sticky": ["settings", "window"],  # #141 stickyTaskbar toggle (ctx.settings.boolean) + the sticky-note window kind
    "aistatus": ["taskbar", "settings", "window"],  # #112 chip + synced settings + window kind
    "git": ["session", "window"],  # #116 per-terminal git widget via ctx.session.git + ctx.windows
    "clipboard": ["clipboard", "window", "taskbar"],  # #106 clipboard seam + window kind; #118 tray chip
    "scratchpad": ["storage", "window"],  # #124 durable server store (ctx.serverStore) + window kind
    "termfont": ["settings", "window"],  # #126 synced termFont select (ctx.settings.select) + per-terminal apply (ctx.windows.onTerminalCreate)
    "recorder": ["window", "settings"],  # #140 per-terminal ⏺ capture (ctx.windows.onTerminalCreate) + library/player window kinds; storage is its own /recording/* (no ctx.file). #151 added the synced recorder.autoRecord toggle (ctx.settings.boolean)
    "host-registry": ["storage", "settings"],  # #65 durable server store (ctx.serverStore) + a browser-mounted registerSettingsPane
    "mousemode": ["window"],  # #155 per-terminal 🖱 chip via ctx.windows.onTerminalCreate; reads xterm's own modes getter, so no other capability
    # #158 browser-mounted registerSettingsPane. NOTE the tier list
    # under-describes this one: the mod also administers a PEER (its #157 mod
    # pins via saveModPins, and its /state mod settings) over hostFetch, and
    # _KNOWN_TIERS has no token for "configures another broker". Deliberately not
    # inventing one -- the vocabulary mirrors the ctx.* capability families, and
    # this mod uses no ctx family beyond settings; the cross-broker reach is
    # core's own host plumbing, which every mod shares.
    "mod-sync": ["settings"],
    # #148 extracted from the tiling core. window: masks/parks windows and adds
    # title-bar menu items; taskbar: badges + dims chips and intercepts
    # activation; settings: owns wsLabelMode + hideTaskbarOtherWs. The desktop
    # seams it uses (ctx.desktop.columnFilter/onColumnCreated/onPlaced/onReveal/
    # onLayoutRender) have no tier token -- like mod-sync's cross-broker reach,
    # the vocabulary mirrors the older ctx families and this one is new.
    "workspaces": ["window", "taskbar", "settings"],
}


def test_mods_manager_pane_and_enable_api_present():
    # #86 (S13): the per-mod enable state (loader-private localStorage), the
    # persist+apply-live setter, and the "Mods" Control Panel pane all ride in the
    # served loader. The Playwright acceptance (list + toggle + master gate) drives
    # these via window.__mods.__test.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    for sym in (
        "'webterm:mods:disabled'",       # the loader-private persistence key
        "function _modsDisabled",
        "function _writeModsDisabled",
        "function isModEnabled",
        "function setModEnabled",
        "window.__mods.masterEnabled",   # master-gate state the live setter honors
    ):
        assert sym in loader, f"missing S13 loader symbol: {sym!r}"
    # #163 moved the pane itself into 86b_js_mod_packages.js, beside the union
    # status model it now renders — 86 was at the 2500-line per-fragment cap.
    pkgs = _packages_src()
    for sym in (
        "function _mountModsManagerPane",
        "set-mods-list",                  # the pane's list container class
        # rows are the UNION of catalog packages and registrations, and they are
        # REBUILDABLE — _modRegisterPane calls spec.render() exactly once, so a
        # row set built before the installed mods registered would be frozen
        # stale and a late registration would never appear.
        "function _rebuildRows",
        "for (const s of _modStatusRows())",
        "window.__mods._rebuildManagerRows = _rebuildRows;",
        # A row with no registration has nothing to init; the checkbox must not
        # pretend otherwise (setModEnabled would refuse it anyway).
        "r.cb.disabled = !s.toggleable;",
        # The pane is built on the S1 pane scaffold (reuse, not a parallel
        # renderer) — which lives in 86 and is called from here.
        "_modRegisterPane(rec, {",
    ):
        assert sym in pkgs, f"missing S13 pane symbol: {sym!r}"
    # The per-mod enable test surface the acceptance drives.
    for sym in ("setEnabled: function", "isEnabled: function",
                "disabledIds: function", "masterEnabled: function"):
        assert sym in loader, f"missing S13 test-API symbol: {sym!r}"
    # And it all reaches the served page (mounts into the existing #set-mods host).
    for sym in ("function setModEnabled", "function _mountModsManagerPane",
                "set-mods-list", 'id="set-mods"'):
        assert sym in INDEX_HTML, f"S13 surface missing from served page: {sym!r}"


def test_mods_manager_pane_styles_present():
    # The pane's core chrome CSS ships in the head <style> (core fragment 15, not a
    # mod stylesheet, so the served page stays free of any mod-css splice).
    css = (BROKER_DIR / "15_css_dialogs.css").read_text(encoding="utf-8")
    for sel in (".set-mods-list", ".set-mod-row", ".set-mod-tier",
                ".set-mod-status"):
        assert sel in css, f"missing S13 pane style: {sel!r}"
    assert ".set-mods-list" in INDEX_HTML
    # #163: every status a runtime-installed package can reach is styled, or a
    # broken package would render as unremarkable muted grey.
    for state in ("loading", "timeout", "blocked-by-cycle", "cycle",
                  "fetch-failed", "no-register", "wrong-id"):
        assert f'.set-mod-status[data-state="{state}"]' in css, \
            f"missing #163 status style: {state!r}"
    # #163 (S5): the provenance badge, the two operator controls, the install
    # dialog's preview chrome, and the per-host pane's self-asserted badge. All
    # in core fragment 15 -- an installed mod's own stylesheet must never be
    # what makes the pane that manages it legible.
    for sel in (".set-mod-source",
                '.set-mod-source[data-mod-source="installed"]',
                ".set-mods-head", ".set-mod-install", ".set-mod-uninstall",
                ".set-mod-actions", ".mod-install-pre", ".mod-install-files",
                ".mod-install-warn", ".mod-install-danger",
                ".set-mod-policy-source"):
        assert sel in css, f"missing #163 S5 style: {sel!r}"
    for sel in (".set-mod-source", ".set-mod-install", ".set-mod-policy-source"):
        assert sel in INDEX_HTML, f"S5 style missing from served page: {sel!r}"


def test_mods_pane_provenance_is_reflected_not_decided():
    # #163 (S5): the shipped/installed badge comes from _modStatusRows(), which
    # reads the BOOT catalog snapshot plus this page's own package records. It
    # is painted by _rebuildRows and never touched by _reflectManager, which
    # runs on EVERY /state pull and must stay read-only -- a badge that
    # re-derived itself there could change under a poll that has nothing to do
    # with mods.
    pkgs = _packages_src()
    for sym in ("function _modSourceBadge(",
                "el.dataset.modSource = s.source;",
                "el.textContent = s.source;",
                "'installed on this broker — v'",
                "label.appendChild(_modSourceBadge(s));"):
        assert sym in pkgs, f"missing S5 provenance symbol: {sym!r}"
    badge = _frag_fn(pkgs, "function _modSourceBadge(")
    # No fetch, no server read: the badge is a pure function of the row.
    for banned in ("hostFetch", "await ", "fetch("):
        assert banned not in badge, \
            f"the provenance badge must not {banned!r} -- it is reflected"
    reflect = _frag_nested_fn(pkgs, "function _reflectManager(")
    assert "_modSourceBadge" not in reflect, \
        "_reflectManager runs on every /state pull; it must not repaint the badge"
    # A mod-supplied version rides a .title PROPERTY (never innerHTML) and is
    # length-clamped -- registerMod's `version` is whatever the mod's object
    # literal said and is capped nowhere else.
    assert "_modClamp(s.version, 32)" in badge
    assert "function _modClamp(" in pkgs


def test_mods_pane_uninstall_is_installed_only_and_worded_verbatim():
    # #163 (S5): Uninstall exists only on an INSTALLED row (a shipped mod lives
    # in the page's own bundle; no broker call can remove it), and the purge
    # checkbox's wording is verbatim from the design -- every clause is
    # load-bearing: three sidecars, broker-side only, and no broker can reach
    # another browser's localStorage.
    pkgs = _packages_src()
    rebuild = _frag_nested_fn(pkgs, "function _rebuildRows(")
    assert "if (s.source === 'installed') {" in rebuild
    assert "un.className = 'set-mod-uninstall';" in rebuild
    wording = ("Also delete this mod's server-side data on this broker (its "
               "/mod-store value and its pin). Data stored in other browsers "
               "is not affected.")
    joined = re.sub(r"['\"]\s*\+\s*['\"]", "", pkgs)
    assert wording in joined, "the purge checkbox wording is not verbatim"
    # Purge is read at COMMIT time off the checkbox itself, not captured when
    # the dialog was built.
    assert "purgeCb.checked" in pkgs


def test_uninstall_never_reports_a_write_failed_purge_as_success():
    # The broker purges DATA FIRST and CODE LAST, so a purge that cannot write
    # answers 500 write_failed WITH THE MOD STILL INSTALLED. Presenting that as
    # any flavour of success inverts the one guarantee that ordering buys.
    pkgs = _packages_src()
    run = _frag_fn(pkgs, "async function _modUninstallPost(")
    assert "if (!r.ok || !j || j.ok !== true) {" in run
    assert "code === 'write_failed'" in run
    assert "is STILL INSTALLED" in run
    # ... and it must NOT claim the data survived either. The purge is three
    # separate sidecar writes and cannot be one transaction, so a failure can
    # land after deleting the /mod-store value and before dropping the pin.
    # "Nothing was removed" would be a guess dressed as a fact.
    assert "may have been PARTIAL" in run
    assert "Nothing was removed" not in run
    # The success bag is only written AFTER the ok check, so a refusal can never
    # grey out the button for a mod that is still there.
    assert run.index("j.ok !== true") < run.index("_modBag('uninstalled')")
    # 404 is documented as ambiguous rather than papered over: uninstall is
    # deliberately not idempotent at the HTTP-result level.
    assert "not_installed" in pkgs


def test_a_lost_response_is_reported_as_unknown_not_as_a_failure():
    # A POST can COMMIT and then lose its answer -- a dropped connection, or a
    # 2xx whose body will not parse. "Nothing was installed" / "is still
    # installed" would be a claim this client cannot make, and it is the claim
    # that gets an operator to retry a mutation that already landed.
    pkgs = _packages_src()
    unknown = _frag_fn(pkgs, "function _modUnknownOutcomeLines(")
    assert "UNKNOWN" in unknown
    for fn in ("async function _modInstallRun(",
               "async function _modUninstallPost("):
        body = _frag_fn(pkgs, fn)
        assert "_modUnknownOutcomeLines(" in body, f"{fn} claims a lost outcome"
        assert "if (r.ok && !parsed) {" in body, \
            f"{fn} treats an unparseable 2xx as a definite refusal"
    # And an install refusal only claims "nothing was written" for the codes
    # that PROVE it -- the validator runs entirely in memory, write_failed does
    # not, and an unrecognised code proves nothing at all.
    proves = _frag_fn(pkgs, "function _modRefusalProvesNoWrite(")
    assert "'write_failed'" not in proves
    assert "'not_installed'" not in proves
    from webterm.broker import modinstall
    for code, status in modinstall.ERROR_STATUS.items():
        if status == 500 or code == "not_installed":
            continue
        assert f"'{code}'" in proves, \
            f"{code} refuses before any write; say so"


def test_a_dialog_whose_body_threw_cannot_commit():
    # openDialog SWALLOWS a throw from spec.body (69_js_dialog.js). A half-drawn
    # preview would otherwise leave an Install button that still sends the
    # payload -- defeating the one promise the dialog makes -- and a half-drawn
    # uninstall would leave Uninstall over a dialog that never showed the purge
    # wording or its checkbox.
    pkgs = _packages_src()
    for fn in ("async function _modInstallPreview(",
               "function _modUninstallDialog("):
        body = _frag_fn(pkgs, fn)
        assert "let bodyOk = false;" in body, f"{fn} has no render flag"
        assert "bodyOk = true;" in body
        assert "if (!bodyOk) {" in body, f"{fn} commits without checking it"


def test_a_cancelled_install_cannot_reopen_over_another_dialog():
    # openDialog is a SINGLETON: a continuation that outlives its own
    # cancellation would later open a dialog OVER -- and so silently cancel --
    # whatever the operator opened instead. Every await boundary in the install
    # flow re-checks the token, and `advanced` distinguishes the handover
    # between stages (which also resolves the previous dialog) from a real
    # cancel.
    pkgs = _packages_src()
    dlg = _frag_fn(pkgs, "function _modInstallDialog(")
    assert "const flow = _modFlow();" in dlg
    assert "if (!flow.live) return null;" in dlg
    assert "if (!flow.advanced) flow.live = false;" in dlg
    preview = _frag_fn(pkgs, "async function _modInstallPreview(")
    assert "if (flow && !flow.live) return;" in preview
    assert "flow.advanced = true" in preview
    # A second POST for the same mod while the first is in flight would race it
    # (one wins, the other 404s, and the singleton leaves only the failure).
    assert "if (_modOpBusy(id)) return;" in _frag_fn(
        pkgs, "async function _modUninstallRun(")
    assert "_modBag('opInFlight')" in pkgs


def test_untrusted_strings_cannot_stall_or_escape_the_pane():
    pkgs = _packages_src()
    # _modClamp slices BEFORE it normalizes: a registration's `version` is
    # whatever its object literal said and is capped nowhere, so normalizing
    # first would scan the whole string on every repaint to show 32 chars.
    clamp = _frag_fn(pkgs, "function _modClamp(")
    assert "v.slice(0, n + 8).replace(" in clamp
    # _modErrorText's table is an object literal, so map['constructor'] is
    # INHERITED and truthy -- a typeof-string check, not truthiness.
    assert "typeof map[code] === 'string'" in _frag_fn(
        pkgs, "function _modErrorText(")
    # The byte count never falls back to String.length (UTF-16 code units would
    # report an emoji as 2 bytes); Blob.size is the UTF-8 length, and -1 means
    # genuinely unknown and renders as such.
    blen = _frag_fn(pkgs, "function _modByteLen(")
    assert "new Blob([text]).size" in blen
    assert "text.length" not in blen
    assert "if (n < 0) return 'size unknown';" in _frag_fn(
        pkgs, "function _modFmtBytes(")
    # A dialog TITLE is a text node in openDialog (head.textContent), and the
    # values that reach it are clamped anyway.
    dialog = (BROKER_DIR / "69_js_dialog.js").read_text(encoding="utf-8")
    assert "head.textContent = spec.title;" in dialog
    assert "'Install ' + (_modClamp(id, 64) || 'this mod')" in pkgs
    assert "'Manifest for ' + _modClamp(one.name, 64)" in pkgs


def test_the_folder_pick_bounds_the_manifest_and_the_warning_list():
    # mod.json is NOT one of the package `files`, so the byte budget does not
    # cover it: without its own bound, a folder holding a small script beside a
    # multi-gigabyte mod.json would be read and JSON.parse'd synchronously on
    # the one UI thread. And a hostile manifest's `scripts`/`styles`/key set can
    # be thousands of entries, i.e. thousands of DOM nodes.
    pkgs = _packages_src()
    folder = _frag_fn(pkgs, "async function _modReadFolder(")
    assert "lim.manifestBytes" in folder
    assert folder.index("lim.manifestBytes") < folder.index("JSON.parse(")
    assert "manifestBytes:" in _frag_fn(pkgs, "function _modPickLimits(")
    warn = _frag_fn(pkgs, "function _modInstallWarnings(")
    assert "raw.slice(0, lim.warnings)" in warn
    raw = _frag_fn(pkgs, "function _modInstallWarningsRaw(")
    assert ".slice(0, 64)" in raw


def test_install_dialog_previews_the_exact_payload():
    # #163 (S5): the preview must not be able to describe a payload other than
    # the one that is sent. So the manifest is rendered as its LITERAL JSON --
    # not a prettified summary -- and the byte counts are the bytes the broker
    # will write (TextEncoder over the decoded text), not File.size, which
    # differs whenever the picked file carried a BOM.
    pkgs = _packages_src()
    preview = _frag_fn(pkgs, "async function _modInstallPreview(")
    assert "JSON.stringify(pick.manifest, null, 2)" in preview
    assert "pre.textContent = json;" in preview
    assert "new TextEncoder().encode(text).length" in _frag_fn(
        pkgs, "function _modByteLen(")
    assert "_modByteLen(pick.files[n])" in preview
    # ... and the POST sends that same object, not a re-derived one.
    run = _frag_fn(pkgs, "async function _modInstallRun(")
    assert "manifest: pick.manifest" in run
    assert "files: pick.files" in run
    assert "'/mods/install'" in run
    # D1: the operator is told the truth about when it takes effect, and is
    # offered the reload that is the only thing which makes it real.
    assert "NEXT page load" in preview
    assert "_modOpResult('Installed', lines, true)" in run
    assert "window.location.reload()" in _frag_fn(pkgs, "function _modOpResult(")
    # #172's residual: state can pre-exist an id, and the operator sees that
    # BEFORE confirming, not only in the response.
    assert "_modAdoptionCheck(id)" in preview
    assert "localStorage cannot be inspected" in pkgs.replace(
        "browser’s own localStorage cannot be inspected",
        "localStorage cannot be inspected")


def test_install_dialog_cannot_walk_a_huge_directory_on_the_ui_thread():
    # A folder pick hands the page EVERY file under the chosen directory, so a
    # mis-click on a home directory is tens of thousands of entries. The refusal
    # is driven off FileList.length -- O(1) -- BEFORE any iteration, and the
    # bytes actually decoded are bounded too. The single event loop is the whole
    # app: a synchronous walk there freezes every terminal on screen.
    pkgs = _packages_src()
    folder = _frag_fn(pkgs, "async function _modReadFolder(")
    assert "if (list.length > lim.entries) {" in folder
    assert folder.index("lim.entries") < folder.index("for (let i = 0")
    assert "if (bytes > lim.readBytes) {" in folder
    limits = _frag_fn(pkgs, "function _modPickLimits(")
    for key in ("entries:", "files:", "readBytes:"):
        assert key in limits, f"missing pick limit: {key!r}"
    # Function-LOCAL, never a fragment-level const (the 86-header TDZ rule).
    assert "        const _MOD_PICK" not in pkgs


def test_every_install_error_code_maps_to_a_sentence():
    # A raw error code in the UI is a bug. This is a drift guard both ways: the
    # broker's code table and the dialog's message table are edited in different
    # files and would otherwise silently diverge the moment a new refusal lands.
    from webterm.broker import modinstall
    text = _frag_fn(_packages_src(), "function _modErrorText(")
    for code in modinstall.ERROR_STATUS:
        assert f"{code}:" in text, \
            f"/mods/* can answer {code!r} and the Mods pane has no wording for it"
    # An unknown code still degrades to a sentence rather than leaking the code
    # alone, and the broker's own `detail` rides a text node, capped.
    assert "this broker refused the request (HTTP" in text
    assert "_modClamp(detail, 400)" in _frag_fn(
        _packages_src(), "function _modFailLines(")


def test_the_install_preview_accepts_every_key_the_broker_accepts():
    # _modManifestKeys drives a WARNING that says "this broker does not accept
    # and will refuse" the keys it does not list -- so a key missing from it is
    # not cosmetic, it tells an operator their CORRECT manifest is doomed. The
    # drift runs both ways: a key listed here that the broker refuses is the
    # same lie in the other direction.
    from webterm.broker import modinstall
    keys = _frag_fn(_packages_src(), "function _modManifestKeys(")
    listed = set(re.findall(r"'([A-Za-z]+)'", keys))
    assert listed == set(modinstall.MANIFEST_KEYS), (
        f"install preview key list drift: only-JS={sorted(listed - set(modinstall.MANIFEST_KEYS))} "
        f"only-broker={sorted(set(modinstall.MANIFEST_KEYS) - listed)}")


def test_the_mods_pane_shows_permissions_and_never_claims_an_unmade_check():
    # #193: the pane shows what a mod is PERMITTED to do beside the tiers it
    # merely claims. Four wire answers, and the whole obligation is that the
    # pane never turns "this broker cannot tell me" into a pass.
    pkgs = _packages_src()
    view = _frag_fn(pkgs, "function _modPermissionsView(")
    # PRESENCE OF THE KEY, never truthiness. `null` (a grandfathered generation
    # on a broker that DOES check) and absent (#157: a broker that predates the
    # check entirely) are different facts, and `if (!catRow.permissions)` would
    # collapse them -- along with `[]`, which is a third, positive one.
    assert "!('permissions' in catRow)" in view
    assert "!catRow.permissions" not in view
    assert "state: 'unchecked'" in view
    assert "Array.isArray(raw)" in view
    assert "state: 'undeclared'" in view
    assert "names.length ? 'declared' : 'none'" in view
    # A row with no catalog entry at all -- an older/headless broker, an /info
    # that 401'd -- is the same "cannot tell you", not an empty declaration.
    assert view.index("!catRow") < view.index("state: 'unchecked'")

    words = _frag_fn(pkgs, "function _modPermissionsText(")
    assert "if (state === 'undeclared') return 'undeclared (pre-lint)';" in words
    assert "if (state === 'none') return 'declares none';" in words
    # The FALLBACK is 'unchecked': an unrecognised state degrades to "we do not
    # know", never to a pass. It is the last statement in the function, so a
    # later branch cannot quietly become the default.
    assert [ln.strip() for ln in words.strip().splitlines() if ln.strip()][-1] \
        == "return 'unchecked';"
    # "none" is the word for an explicit [], and only for that. An ABSENT
    # declaration is never described as one -- that is the false positive claim
    # this whole issue exists to stop.
    assert "'none'" not in _modPermissionsBranch(words, "undeclared")

    # And nothing in the display claims a verification. Absence is unchecked.
    display = words + _frag_fn(pkgs, "function _modPermissionsTitle(")
    for banned in ("verified", "verify", "safe", "trusted"):
        assert banned not in display.lower(), \
            f"the permissions display claims {banned!r}; it is entitled to none"

    # The row carries it, and takes it from the CATALOG ROW only: registerMod
    # deliberately ships no copy of `permissions`, because a runtime copy
    # nothing guarantees to match the reviewed manifest is worse than none.
    row = _frag_fn(pkgs, "function _modStatusRow(")
    assert "permissions: _modPermissionsView(catRow)" in row
    assert "_modPermissionsView(decl" not in pkgs
    assert "decl.permissions" not in pkgs

    # ... and the pane RENDERS it, for shipped and installed rows alike -- the
    # chips are built unconditionally, beside the tiers, with no source test.
    rebuild = _frag_nested_fn(pkgs, "function _rebuildRows(")
    assert "s.permissions.state" in rebuild
    assert "_modPermissionsText(" in rebuild
    assert "row.appendChild(perms);" in rebuild
    assert "perms.dataset.permState = s.permissions.state;" in rebuild
    assert rebuild.index("row.appendChild(tiers);") \
        < rebuild.index("row.appendChild(perms);")
    assert "s.source" not in rebuild[rebuild.index("const perms ="):
                                     rebuild.index("row.appendChild(perms);")]


def _modPermissionsBranch(words, state):
    """The one line of _modPermissionsText that answers ``state``."""
    return next(ln for ln in words.splitlines() if f"'{state}'" in ln)


def test_per_mod_enable_is_loader_private_not_state_schema():
    # The per-mod enable is deliberately PER-BROWSER (localStorage), NOT a synced
    # /state settings field: it must not have leaked a new key into the backend
    # /state normalizer (the inherited "no schema change for new keys" rule), and
    # the loader documents the per-browser choice.
    settings = (BROKER_DIR / "55_js_settings_model.js").read_text(encoding="utf-8")
    assert "modsDisabled" not in settings
    assert "webterm:mods:disabled" not in settings
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "PER-BROWSER" in loader


def test_loadmods_honors_per_mod_disabled_under_master_gate():
    # Boot skips a per-mod-disabled mod, but ONLY after the master gate passes
    # (master off still returns first => every mod off). The pane mounts only when
    # the master gate is on, so master-off means no mod UI at all.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    boot = loader[loader.index("async function loadMods"):]
    # master gate returns BEFORE the pane mount + the per-mod skip. #112/#116: the
    # boot now gates on the EFFECTIVE per-mod state (isModEnabled, which honors a
    # mod's declared defaultEnabled) rather than raw disabled-set membership, so a
    # default-off mod (aistatus #112, git #116) does not init at boot until opted in.
    assert boot.index("mods_enabled=false") < boot.index("_mountModsManagerPane()")
    assert boot.index("_mountModsManagerPane()") < boot.index("isModEnabled(decl.id)")
    # stale ids are pruned so the set can't grow junk.
    assert "pruned" in boot
    # #157: the broker's PINS resolve before the gate and before any init, so
    # every later reader (the boot loop, the Mods pane, setModEnabled) sees one
    # frozen answer and no mod can start under the wrong policy.
    assert boot.index("_resolvePins(") < boot.index("mods_enabled=false")
    assert boot.index("_resolvePins(") < boot.index("isModEnabled(decl.id)")


def test_mod_policy_pins_outrank_the_per_browser_toggle():
    # #157 resolution order: a pin from the broker's policy wins over the
    # per-browser set, and a pinned mod refuses the local toggle outright (so a
    # programmatic caller can't persist a preference the resolver ignores).
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    enabled = loader[loader.index("function isModEnabled"):
                     loader.index("function _bringUp")]
    # The pin is consulted and returned BEFORE the disabled-set is even read.
    assert enabled.index("_pin(id)") < enabled.index("_modsDisabled()")
    setter = loader[loader.index("function setModEnabled"):
                    loader.index("// The boot entry")]
    assert setter.index("_pin(id)") < setter.index("_writeModsDisabled(")
    # A pinned-ON mod's dependencies are pinned too, or "pinned on" is a lie the
    # moment a browser has the dependency locally disabled (#121 leaves the
    # dependent blocked, and the disable cascade would tear it down again).
    pins = loader[loader.index("function _resolvePins"):
                  loader.index("function _pin(")]
    assert "requires" in pins and "explicit" in pins


def test_mod_policy_is_broker_admin_state_not_the_synced_state_blob():
    # #157: a /state PUT is lease-gated (409 not_active for any browser that is
    # not that broker's active view), so a policy stored in the settings blob
    # could never be changed on a broker with a live viewer -- the very broker
    # this section exists to administer. The policy therefore lives in the
    # broker's own sidecar behind POST /mods/policy, and NOTHING may write it
    # through the settings target.
    settings = (BROKER_DIR / "55_js_settings_model.js").read_text(encoding="utf-8")
    assert "modPolicy" not in settings
    panel = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    policy = panel[panel.index('// ---- "Mods on this broker" section'):
                   panel.index("// Hosts whose last /profiles/config GET failed")]
    assert "'/mods/policy'" in policy and "'/info'" in policy
    # Never through t.s / t.save() (the lease-gated /state path every other
    # control on this pane uses).
    assert "t.save()" not in policy and "putHostState" not in policy
    # The host is resolved at EVENT time inside the change handler, because
    # hostFetch(null, path) silently resolves against OUR OWN origin -- a stale
    # closure would write a peer's pin into this broker's policy.
    assert "hostById(currentSettingsTab)" in policy
    # Every failure mode gets its own copy, and none of them re-fetches on a
    # repaint (a sleeping broker must not become a hot retry loop).
    for state in ("headless", "unsupported", "unauthorized", "unreachable"):
        assert state in policy
    assert "modCatalogFetching" in policy


def test_mod_policy_section_is_per_host_and_painted_on_tab_switch():
    # The section is STATIC core markup in the host pane, NOT a loader-mounted
    # pane: it must survive a local broker whose own mods_enabled is false (which
    # makes loadMods return before mounting anything), and it must not be
    # .set-browser-global or applyBrowserGlobalVisibility would hide it on every
    # remote tab -- the tabs it exists for.
    body = (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    # The ELEMENT, not the comment above it (which explains why it is not global).
    # Sliced from the opening tag's id (#181 added a data-applet to it), so the
    # assertion below is about the class list rather than the attribute order.
    section = body[body.rindex("<div", 0, body.index('id="set-mod-policy"')):
                   body.index('id="set-mod-policy-hint"')]
    assert 'class="set-section"' in section
    assert "set-browser-global" not in section
    assert 'id="set-mod-policy"' in body and 'id="set-mod-policy-list"' in body
    assert body.index('id="set-mod-policy"') > body.index('id="set-pane-host"')
    assert body.index('id="set-mod-policy"') < body.index('id="set-pane-browser"')
    # Painted SYNCHRONOUSLY on tab select, before the remote /state await: a slow
    # or failing fetch must never leave the previous host's rows on screen under
    # this host's name (the #153 lesson, 81:205-212).
    panel = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    tab = panel[panel.index("async function selectSettingsTab"):
                panel.index("// Populate the host-form fields")]
    assert tab.index("renderModPolicy()") < tab.index("await fetchHostState")
    assert tab.index("renderModPolicy()") < tab.index("if (tabId === 'browser')")
    # #163 (S5): a peer's provenance is SELF-ASSERTED. It is rendered as a quote
    # and anything unrecognised -- a missing key, a non-string, a value from a
    # future build, a value a hostile broker invented -- falls back to `unknown`
    # and NEVER to the more-trusted-looking label. "unknown => shipped" would
    # let a broker present a third-party package as part of our own bundle just
    # by omitting one field.
    src = _frag_fn(panel, "function peerModSource(")
    assert "if (source === 'installed') return 'peer reports: installed';" in src
    assert "if (source === 'shipped') return 'peer reports: shipped';" in src
    assert src.rstrip().endswith("return 'unknown';"), \
        "the fallback must be `unknown`, and it must be the LAST word"
    assert "src.textContent = peerModSource(m.source);" in panel
    # Peer rows are unique-id enforced: two rows for one id would give one mod
    # two selects writing the same key, so the value on screen would depend on
    # which row was touched last. And the implied-pin index is prototype-free,
    # so an id of "__proto__" cannot make every mod read as implied-on.
    render = _frag_fn(panel, "function renderModPolicy(")
    assert "if (served.has(m.id)) continue;" in render
    assert "const implied = Object.create(null);" in \
        _frag_fn(panel, "function modPolicyImplied(")
    # `missing` is OUR sentinel, never a wire field: a peer sending
    # `missing:true` on a mod it DOES serve would suppress its own provenance
    # badge and collect the "not installed on this broker" note -- a served mod
    # dressed as an absent one. The row is rebuilt from named fields.
    assert "missing: false });" in render
    assert "rows.push(m);" not in render
    # The install control is deliberately NOT here: installing on a broker
    # somebody else runs is a separate trust decision (design 11).
    assert "/mods/install" not in panel
    # ... and the uninstall-residue note the migration story leans on stays.
    assert "not installed on this broker" in render


def test_mod_policy_applies_after_a_first_login():
    # A browser arriving with no stored token 401s on the boot /info, so the pins
    # are unknown and every mod comes up at this browser's own default -- and
    # entering the token heals sockets in place, it does NOT reload. The login
    # success path therefore re-asks once and reconciles, or the broker's "pinned
    # for every browser that loads its page" promise is false on first visit.
    auth = (BROKER_DIR / "63_js_clipboard_auth.js").read_text(encoding="utf-8")
    assert "notifyModsHostAuth(host.id)" in auth
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    hook = loader[loader.index("async function notifyModsHostAuth"):
                  loader.index("// Test API")]
    # Narrow by construction: HOME broker only, never once the snapshot is
    # authoritative (so an ordinary later round trip can't re-tear-down a mod in
    # use), and it re-fetches rather than trusting the memo.
    assert "policyAuthoritative" in hook
    assert "hostId !== lh.id" in hook
    assert "localInfo(true)" in hook


def test_mods_declare_reviewed_trust_tiers():
    # Every in-repo mod declares a `tiers:` array in its registerMod, the values
    # are from the known vocabulary, and they match the reviewed expectation for
    # that mod (derived from its actual ctx usage). This is the declared-tier drift
    # guard the "Mods" pane's permission review depends on.
    import re
    for mod_dir in dict.fromkeys(
            PurePosixPath(m).parent.as_posix() for m in ui._MODS):
        mod_id = PurePosixPath(mod_dir).name
        if mod_id not in _EXPECTED_TIERS:
            continue  # codemirror.js shares the editor dir; only registrants count
        # The registerMod-bearing script is the one named <id>.js.
        src = (BROKER_DIR / mod_dir / f"{mod_id}.js").read_text(encoding="utf-8")
        m = re.search(r"id:\s*'%s'.*?tiers:\s*\[([^\]]*)\]" % re.escape(mod_id),
                      src, re.S)
        assert m, f"mod {mod_id!r} must declare a tiers: [...] array in registerMod"
        tokens = re.findall(r"'([a-z-]+)'", m.group(1))
        assert tokens, f"mod {mod_id!r} declared an empty tiers array"
        assert set(tokens) <= _KNOWN_TIERS, (
            f"mod {mod_id!r} declares unknown tier(s): "
            f"{sorted(set(tokens) - _KNOWN_TIERS)}")
        assert tokens == _EXPECTED_TIERS[mod_id], (
            f"mod {mod_id!r} tiers drifted: declared {tokens}, "
            f"reviewed {_EXPECTED_TIERS[mod_id]}")


# --------------------------------------------------------------------------- #
# control-panel secrets are masked text, not native password inputs (#99)
# --------------------------------------------------------------------------- #

def test_control_panel_has_no_native_password_inputs():
    # #99: closing the Control Panel reparents its #settings-modal subtree (with a
    # populated MCP token field, next to text "username" fields) into a hidden
    # overlay, which Chromium reads as a completed login and offers to "Save
    # password?". The root-cause fix removes password-field classification: the two
    # control-panel secrets are now masked type=text inputs, so the password
    # manager can't engage. Lock that no native password input survives in the
    # panel — neither the old markup (regression) nor any future one.
    for old in ('type="password" id="set-mcp-token"',
                'type="password" id="set-host-pass"'):
        assert old not in INDEX_HTML, \
            f"control-panel secret regressed to a native password input: {old!r}"
    # Both secrets ship as masked text inputs (CSS-masked, .value read identically).
    for masked in ('type="text" id="set-mcp-token" class="masked-secret"',
                   'type="text" id="set-host-pass" class="masked-secret"'):
        assert masked in INDEX_HTML, f"masked secret input missing: {masked!r}"
    # The visual mask rides the assembled CSS.
    assert "-webkit-text-security: disc" in INDEX_HTML, \
        "masked-secret CSS mask missing from the served page"
    # The ONLY native password input left anywhere is the separate #auth-form
    # re-auth field (out of scope — a real login form where saving may be wanted).
    import re
    pw_ids = re.findall(r'type="password"\s+id="([^"]+)"', INDEX_HTML)
    assert pw_ids == ["auth-token"], \
        f"unexpected native password input(s) survive: {pw_ids}"


# --------------------------------------------------------------------------- #
# the Control Panel is an applet grid (#181)
# --------------------------------------------------------------------------- #

_APPLET_FRAGMENT = "81a_js_control_panel_applets.js"


def _applet_ids():
    """The applet ids declared in the CP_APPLETS table, in table order."""
    src = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    table = src[src.index("const CP_APPLETS = ["):src.index("const CP_APPLET_INDEX")]
    return re.findall(r"\{ id: '([a-z-]+)'", table)


def test_applet_grid_table_reaches_the_served_page():
    # The table is the whole navigation: an applet that never reaches the page is
    # a topic nobody can open. Assert the ids, their captions, their status-line
    # blurbs AND their icons all ride the assembled HTML.
    ids = _applet_ids()
    assert len(ids) == len(set(ids)), f"duplicate applet id in CP_APPLETS: {ids}"
    assert set(ids) == {"desktop", "windows", "input", "terminals", "startup",
                        "access", "mods", "advanced"}
    src = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    icons = src[src.index("const CP_APPLET_ICON_SVG = {"):
                src.index("function cpAppletIconSvg")]
    for aid in ids:
        assert f"'{aid}'" in icons, f"applet {aid!r} has no icon table entry"
        assert f"id: '{aid}'" in INDEX_HTML, f"applet {aid!r} missing from the page"
    # The status line is what makes an icon grid navigable rather than a guessing
    # game, so every applet declares one. It is the piece most likely to be
    # dropped as "polish"; it is not.
    blurbs = re.findall(r"blurb: '[^']", src)
    assert len(blurbs) == len(ids), \
        f"{len(ids)} applets but {len(blurbs)} status-line blurbs"
    # Core applets need their OWN icons: APP_ICON_SVG (65) is keyed by app KIND
    # and deliberately closed, and "Windows"/"Input"/"Startup" are not app kinds.
    theming = (BROKER_DIR / "65_js_display_theming.js").read_text(encoding="utf-8")
    closed = theming[theming.index("const APP_ICON_SVG = {"):
                     theming.index("function appIconSvg")]
    for aid in ids:
        assert f"'{aid}':" not in closed, \
            f"applet {aid!r} leaked into the closed app-kind icon table"


def test_every_control_panel_section_belongs_to_exactly_one_applet():
    # The failure mode this guards is a setting that becomes UNREACHABLE: behind
    # eight icons, a section with no applet is drawn by no tile. Every .set-section
    # in #set-pane-host must carry a data-applet naming a known applet -- except
    # #set-mods, which is scaffolding (mods append their own sections INTO it) and
    # is toggled by the router as a container, not as a member.
    body = (BROKER_DIR / "40_body.html").read_text(encoding="utf-8")
    pane = body[body.index('id="set-pane-host"'):body.index('id="set-pane-browser"')]
    ids = set(_applet_ids())
    sections = re.findall(r'<div class="set-section[^"]*"([^>]*)>', pane)
    assert len(sections) >= 17, \
        f"expected the full host pane, found {len(sections)} sections"
    orphans, unknown = [], []
    for attrs in sections:
        if 'id="set-mods"' in attrs:
            assert "data-applet" not in attrs, \
                "#set-mods is a container, not an applet member"
            continue
        m = re.search(r'data-applet="([^"]+)"', attrs)
        if not m:
            orphans.append(attrs.strip())
        elif m.group(1) not in ids:
            unknown.append(m.group(1))
    assert not orphans, f"section(s) with no applet (unreachable): {orphans}"
    assert not unknown, f"section(s) naming an unknown applet: {unknown}"
    # Exactly one applet each: a second data-applet on one tag would make the
    # membership depend on which one the parser kept.
    assert pane.count("data-applet=") == len(sections) - 1


def test_applet_router_never_writes_inline_display():
    # #17/#178, restated: `.set-browser-global` already has TWO writers of inline
    # style.display (applyBrowserGlobalVisibility, and _controlSection at create
    # time), inline beats every selector, and last-writer-wins there puts controls
    # bound to the live LOCAL settings on a remote broker's tab. So the applet
    # router and the filter hide through CLASSES and nothing else.
    src = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    assert not re.search(r"style\.display\s*=[^=]", code), \
        "the applet router became a third inline-display writer"
    for cls in ("cp-applet-off", "cp-filter-off"):
        assert f"classList.toggle('{cls}'" in src, f"{cls} is not class-driven"
        assert f"#set-pane-host .{cls}" in INDEX_HTML \
            or f".{cls}," in INDEX_HTML, f"{cls} has no CSS rule on the page"
    # Availability is read off the INLINE property, never getComputedStyle --
    # which our own hiding classes would pollute into a circular answer.
    assert "getComputedStyle" not in code
    assert "el.style.display !== 'none'" in code


def test_flat_view_is_the_same_sections_not_a_second_renderer():
    # "Show everything" falls back to the flat scroll by dropping the applet
    # class, NOT by rendering a parallel list -- otherwise every future section
    # has to be added in two places and the two drift.
    src = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    # The only mention of the class is the query that FINDS the existing nodes.
    assert code.count("set-section") == 1 \
        and "querySelectorAll('.set-section')" in code, \
        "the applet fragment builds .set-section nodes of its own"
    # Rendering stays EAGER: opening an applet triggers no render (the #153/#157
    # lesson -- a slow or failed remote /state must never leave the previous
    # broker's rows under this host's name).
    for renderer in ("renderSettings(", "renderModPolicy(", "renderKeybindings(",
                     "renderProfilesEditor(", "renderMcpConfig("):
        assert renderer not in code, \
            f"{renderer} is called from the applet router -- render must stay eager"


def test_applet_view_state_is_browser_local_and_survives_reset_local_view():
    # The "Show everything" toggle is a VIEW preference: two people viewing one
    # broker must not fight over it, so it must not ride the synced blob.
    src = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    assert "const CP_PREFS_KEY = '_controlPanel'" in src
    # (a) outside prefs._settings / prefs._layout, the only two things _stateBlob
    #     serializes, so savePrefs can never push it;
    sync = (BROKER_DIR / "52_js_state_sync.js").read_text(encoding="utf-8")
    blob = sync[sync.index("function _stateBlob()"):sync.index("_stateSerialize")]
    assert "_controlPanel" not in blob
    # (b) UNDERSCORE-prefixed, because "Reset local view" deletes every
    #     non-underscore top-level pref as per-session window geometry;
    ident = (BROKER_DIR / "83_js_broker_identity.js").read_text(encoding="utf-8")
    reset = ident[ident.index("function resetLocalView()"):]
    assert "k.charAt(0) === '_'" in reset[:400], \
        "resetLocalView no longer spares underscore keys -- recheck CP_PREFS_KEY"
    # (c) persisted with savePrefsLocal, so toggling a view preference does not
    #     schedule a broker PUT of unrelated settings/layout.
    setter = src[src.index("function cpSetFlatMode"):src.index("function cpFilterQuery")]
    assert "savePrefsLocal()" in setter and "savePrefs()" not in setter
    # Strict === true: a corrupted stored "false" must not enable flat mode.
    assert "p.flat === true" in src
    # The filter box holds no state worth persisting and resets per open.
    assert "filt.value = ''" in src


def test_mod_sections_take_a_closed_applet_id_from_their_existing_mount():
    # #181 rides _controlSection's EXISTING opts.mount seam: already a per-call
    # placement hint resolved to a DOM host, already falling back to the shared
    # bucket on an unknown value, already funnelled through by every
    # ctx.settings.* primitive AND registerSettingsPane.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    sect = loader[loader.index("function _controlSection"):
                  loader.index("function _modTrack")]
    assert "cpAppletFor(opts.mount)" in sect
    # A 'browser' mount is resolved FIRST and gets no applet (different pane).
    assert sect.index("browserMount") < sect.index("cpAppletFor")
    assert "if (!browserMount) section.dataset.applet" in sect
    # The DOM host stays #set-mods whatever the applet is, so the silent-drop
    # branch (`if (host) host.appendChild`) stays unreachable: an applet id must
    # never resolve to a null host.
    assert "'set-browser-mods' : 'set-mods'" in sect
    # Closed set, owned by core: an unknown id degrades, it never MINTS an applet
    # (a typo would otherwise mint a one-item applet titled by the typo).
    applets = (BROKER_DIR / _APPLET_FRAGMENT).read_text(encoding="utf-8")
    resolver = applets[applets.index("function cpAppletFor"):
                       applets.index("const CP_MOD_BADGE_GLYPH")]
    assert "CP_APPLET_INDEX[hint]" in resolver and "CP_DEFAULT_APPLET" in resolver
    # registerSettingsPane forwards the hint verbatim instead of flattening it.
    # (#194 moved the help-card family out of the loader, so this slice now ends
    # at the section that follows _modRegisterPane rather than at the sanitizer's
    # block-type table.)
    pane = loader[loader.index("function _modRegisterPane"):
                  loader.index("function _modRegisterWindowKind")]
    assert "mount: browserMount ? 'browser' : spec.mount" in pane
    # A mod placed inside a CORE applet stays attributable (#163: an installed
    # mod is not first-party code). The badge is the SAME {svg}|{text} trust
    # split #170 drew -- trusted table or our own glyph, never a mod's string.
    badge = applets[applets.index("function cpModBadge"):
                    applets.index("let cpOpenApplet")]
    assert "appIconSvg(modId)" in badge and "CP_MOD_BADGE_GLYPH" in badge
    assert badge.count("innerHTML") == 1, \
        "cpModBadge grew a second innerHTML sink"


def test_remote_load_placeholder_is_cleared_on_every_exit_path():
    # Only the REMOTE branch of selectSettingsTab raises #set-host-loading, and
    # only its own success path lowered it -- so leaving a FAILED remote tab
    # stranded "could not load this broker's settings" at the top of a pane it no
    # longer describes (for local, for Browser, and across a close/reopen of the
    # borrowed singleton). #181 did not cause that, but made it conspicuous: in
    # grid mode the pane is otherwise empty, so the stale line sits alone above
    # the applet icons.
    panel = (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")
    assert "function clearHostLoading()" in panel
    # Resets the TEXT too -- the failure branch overwrites it, so the next load
    # would otherwise open showing the previous failure.
    fn = panel[panel.index("function clearHostLoading()"):
               panel.index("// Switch tabs.")]
    assert "style.display = 'none'" in fn
    assert "loading settings" in fn
    assert "classList.remove('loading')" in fn
    # Called unconditionally at the top of every tab switch, BEFORE the remote
    # branch raises it again for its own fetch...
    tab = panel[panel.index("async function selectSettingsTab"):
                panel.index("// Populate the host-form fields")]
    assert tab.index("clearHostLoading()") < tab.index("if (tabId === 'browser')")
    assert tab.index("clearHostLoading()") < tab.index("setHostLoadingEl.style.display = ''")
    # ...and on open, where the tab is known to be local.
    opener = panel[panel.index("function openControlPanelWindow"):
                   panel.index("function toggleControlPanelWindow")]
    assert "clearHostLoading()" in opener
    # The applet view is reset on open too, and AFTER renderSettings -- which
    # repaints every section (and reconciles) before we decide what to show.
    assert opener.index("renderSettings()") < opener.index("cpResetControlPanelView()")


def test_bevel_vars_have_a_static_value_outside_the_color_mix_gate():
    # #173's finding, at the custom-property layer: a custom property accepts an
    # ARBITRARY token stream, so `--x: <static>; --x: color-mix(...)` does NOT
    # fall back on an engine without color-mix -- the second declaration still
    # wins and the CONSUMING property becomes invalid at computed-value time,
    # i.e. `unset`. Chiselled edges would then be absent, not "slightly wrong".
    # The static pair must therefore live in :root and the derived pair behind an
    # @supports gate.
    css = (BROKER_DIR / "10_css_root.css").read_text(encoding="utf-8")
    at = css.index("@supports (color: color-mix(")
    root, gate = css[css.index(":root {"):at], css[at:]
    for var in ("--bevel-light", "--bevel-dark", "--bevel-face"):
        assert re.search(rf"{var}:\s*#[0-9a-f]{{6}};", root), \
            f"{var} has no static value in :root"
        assert "color-mix" not in root.split(var + ":")[-1].split(";")[0], \
            f"{var}'s :root value must be static, not a color-mix"
        assert f"{var}: color-mix(" in gate, \
            f"{var}'s derived value is not inside the @supports gate"
    assert "@supports (color: color-mix(" in css
    # And nothing hardcodes grey for the dress: the bevels are derived from the
    # theme's own --bg-2 so the panel is coherent under every scheme.
    assert "var(--bg-2)" in gate


# --------------------------------------------------------------------------- #
# floating windows honour the active workspace (#152)
# --------------------------------------------------------------------------- #

def test_float_workspace_placement_helpers_wired_into_page():
    # #152: floating membership was never written at creation (so a window born
    # on a non-active workspace painted, then flicked away when the 2 s poll
    # stamped it) and never cleared on close (so prefs._floatWs only grew and a
    # fixed id inherited dead membership). No JS test runner exists (pytest
    # only), so lock the served-page symbols of the fix.
    for sentinel in (
        "function windowOffActiveWs",      # membership test, not a CSS-class test
        "function adoptFloatWorkspace",    # stamp + mask in the creation frame
        "function finishWindowPlacement",  # the one factored creation tail
        "function revealAndFocusWindow",   # the one factored open-or-focus
    ):
        assert sentinel in INDEX_HTML, f"missing #152 sentinel: {sentinel!r}"


def test_float_workspace_decided_from_membership_not_css_class():
    # The reveal must key off MEMBERSHIP (prefs._floatWs vs the active workspace),
    # never off the .ws-hidden class: the class is a presentation echo that a
    # setWindowWs(render=false) leaves stale in EITHER direction, so a class-only
    # test both misses a float that must move and refuses to repair one that must
    # not. windowOffActiveWs is the predicate that encodes it.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    start = ws.index("function windowOffActiveWs")
    body = ws[start:ws.index("function adoptFloatWorkspace")]
    assert "ws-hidden" not in body,         "windowOffActiveWs must not consult the ws-hidden class"
    assert "activeWorkspaceId()" in body


def test_creation_tails_are_factored_through_finish_window_placement():
    # Every window factory ended with the same two lines; they are now one call,
    # so the workspace stamp can never be forgotten by a new factory that copies
    # the tail. The old form must be gone from every factory.
    factories = [
        "81_js_control_panel.js",
        "mods/aistatus/aistatus.js",
        "mods/clipboard/clipboard.js",
        "mods/editor/editor.js",
        "mods/file-manager/file-manager.js",
        "mods/help/help.js",
        "mods/recorder/recorder.js",
        "mods/scratchpad/scratchpad.js",
        "mods/task-manager/task-manager.js",
    ]
    for rel in factories:
        text = (BROKER_DIR / rel).read_text(encoding="utf-8")
        assert "finishWindowPlacement(win);" in text,             f"{rel} does not use the factored creation tail"
        assert "if (findKeyInLayout(id)) placeWindowTiled(win);" not in text,             f"{rel} still carries the old unstamped creation tail"
    # openWindow (terminals) keeps its own decideTiled split — placement happens
    # before the RAF measurement — so it ANNOUNCES the float placement in the
    # else branch. #148 moved the workspace stamp itself into the mod: core says
    # "this was placed as a float" and whoever cares (the workspaces mod, via
    # ctx.desktop.onPlaced) masks it in that same frame.
    life = (BROKER_DIR / "67_js_window_lifecycle.js").read_text(encoding="utf-8")
    assert "notifyWindowPlaced(win);" in life
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    assert "ctx.desktop.onPlaced(adoptFloatWorkspace);" in ws


def test_close_window_forgets_float_workspace_membership():
    # The prefs GC skips every _-prefixed key, so closeWindow is the ONLY pruning
    # prefs._floatWs gets. Without it the map grows without bound and a window
    # with a fixed id (app:recorder / app:clip / app:scratch) reopens onto the
    # workspace a previous instance died on.
    #
    # #148: core no longer names the map. closeWindow announces that the KEY is
    # gone for good and the workspaces mod prunes its own bookkeeping — so the
    # invariant is now "core announces exactly once, the mod deletes".
    rt = (BROKER_DIR / "73_js_window_runtime.js").read_text(encoding="utf-8")
    close = rt[rt.index("function closeWindow"):rt.index("async function requestCloseAppWindow")]
    assert "notifyWindowForgotten(id);" in close
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    forgotten = ws[ws.index("ctx.desktop.onForgotten"):]
    assert "delete floatWsMap()[key];" in forgotten[:400]
    assert "savePrefsLocal();" in forgotten[:400]
    # teardownView (remote-lease loss) must NOT route through closeWindow, or a
    # rebuild would re-home every window to the active workspace.
    alv = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    teardown = alv[alv.index("function teardownView"):alv.index("async function rebuildView")]
    assert "closeWindow(" not in teardown,         "teardownView must inline its teardown so membership survives a rebuild"


def test_revealed_float_refits_its_terminal():
    # .ws-hidden is display:none, so a masked window has a 0x0 box and sendResize
    # bails on it. A window created already-masked therefore never sent ANY
    # cols/rows; one revealed by a workspace switch kept stale ones. The
    # hidden -> visible transition has to re-measure.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    vis = ws[ws.index("function applyWorkspaceVisibility"):
             ws.index("function workspaceIndexForKey")]
    assert "refitSoon(win)" in vis,         "a float revealed by a workspace switch must re-measure"


def test_taskbar_chip_click_keeps_its_own_reveal():
    # onTaskbarClick is the path #152 factored the helper OUT of, but it keeps its
    # inline reveal on purpose: its `revealed` flag feeds the minimize-toggle
    # guard below it, so a chip click on an off-workspace window reveals instead
    # of round-tripping straight back to minimized.
    tb = (BROKER_DIR / "75_js_taskbar_hosts.js").read_text(encoding="utf-8")
    click = tb[tb.index("function onTaskbarClick"):tb.index("// Hard ceiling on how long")]
    assert "let revealed = false;" in click
    assert "if (!switched && !revealed && frontId === id) { minimizeWindow(id); return; }" in click
def test_app_launch_rehomes_but_restore_keeps_its_workspace():
    # The creation path needs the same split as the dedupe path. A FIXED-id
    # singleton keeps its prefs._floatWs entry long after its window is gone —
    # the ephemeral kinds (clipboard, recorder library) are never recreated by
    # restoreAppWindows, and a reload drops the window while localStorage keeps
    # the entry. Building it masked and letting bringToFront refuse it is
    # symptom B again, via creation instead of dedupe. So openAppWindow reveals
    # after building, EXCEPT for its one automatic caller.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    assert "function openAppWindow(appData, opts)" in s54
    assert "const restoring = !!(opts && opts.restoring);" in s54
    assert "if (win && !restoring) revealAndFocusWindow(win.id);" in s54
    assert "if (!restoring) revealAndFocusWindow(id);" in s54
    # restoreAppWindows is that automatic caller, and must say so — otherwise
    # every persisted window re-homes to whichever workspace the page boots on.
    alv = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    restore = alv[alv.index("function restoreAppWindows"):alv.index("// Restore-on-refresh")]
    assert "{ restoring: true }" in restore,         "restoreAppWindows must mark itself as a restore, not a launch"


def test_adopt_float_workspace_heals_dangling_membership():
    # applyWorkspaceVisibility heals a membership pointing at a removed workspace.
    # The creation stamp must heal it the same way, or a window whose workspace
    # was deleted is masked against a workspace nothing can switch to and only
    # appears when the next poll runs — "does nothing, then appears later", which
    # is worse than the flicker #152 set out to fix.
    # #148: finishWindowPlacement / revealAndFocusWindow are CORE (62a) now;
    # adoptFloatWorkspace is the workspace-side hook they call into.
    ws = (BROKER_DIR / "mods/workspaces/workspaces.js").read_text(encoding="utf-8")
    body = ws[ws.index("function adoptFloatWorkspace"):
              ws.index("function applyWorkspaceVisibility")]
    assert "liveWsIds" in body, "adoptFloatWorkspace must heal a dangling ws id"
    assert "applyTaskbarWorkspace()" in body,         "a freshly stamped window's taskbar ws badge must not lag a poll"


def test_app_window_restore_retries_after_the_mod_loader():
    # #167: restore and the mod loader are independent async chains (the lease +
    # the /state adopt vs GET /info). When restore wins, a persisted mod-owned
    # kind (file-manager / scratchpad) is not registered yet, buildAppWindow
    # returns null and the window silently vanishes for the whole page load.
    alv = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    block = alv[alv.index("function restoreAppWindows"):alv.index("// Restore-on-refresh")]
    # The generation restore records the ids the race skipped, and ONLY those:
    # a null from a kind that WAS registered is a failure, not a race, and a
    # builder that THREW must be told apart from the deliberate null (else a
    # partial build is replayed on every later mod enable).
    assert "if (!kind && !win && !threw && deferred) deferred.add(appId);" in block
    assert "catch (e) { threw = true;" in block
    # ...published for exactly one _viewEpoch, captured at ENTRY (mod code in a
    # factory can synchronously bump it), and never by a superseded pass.
    assert "const epoch = _viewEpoch;" in block
    assert "if (!_deactivated && epoch === _viewEpoch) {" in block
    assert "_deferredRestore = { epoch: epoch, ids: deferred };" in block
    # ...and drained immediately, because the set does not exist until that line:
    # a kind registered DURING the loop raced past the cursor with nothing left
    # to trigger it.
    assert block.index("_deferredRestore = { epoch: epoch, ids: deferred };") <         block.index("restoreAppWindowsAfterMods();")
    # The retry re-attempts exactly that set.
    retry = block[block.index("function restoreAppWindowsAfterMods"):]
    assert "pending.epoch !== _viewEpoch" in retry
    assert "pending.ids.delete(appId);" in retry,         "an id must be unreachable while its own build is in flight"
    assert "_restoreOneAppWindow(appId, pending.ids);" in retry


def test_restore_retry_guards_on_state_ready_not_just_booted():
    # Constraint 4 of #167: bootActiveView sets _booted BEFORE its
    # `await _stateReadyPromise`, so keying the retry on _booted alone could
    # restore against a layout the /state adopt has not applied yet.
    alv = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    retry = alv[alv.index("function restoreAppWindowsAfterMods"):
                alv.index("// Restore-on-refresh")]
    assert "if (_deactivated) return;" in retry
    assert "if (!_stateReady) return;" in retry
    code = "\n".join(ln for ln in retry.splitlines()
                     if not ln.lstrip().startswith("//"))
    assert "_booted" not in code, "the retry must not key on _booted"
    # ...and a re-entrancy latch, because a factory is mod code that can reach
    # back into the loader (setModEnabled -> _bringUp -> this) mid-build.
    assert "if (_restoreRetrying) { _restoreRetryAgain = true; return; }" in retry,         "a request arriving while busy must set a wakeup, not be swallowed"
    assert "_restoreRetrying = true;" in retry
    assert "} finally { _restoreRetrying = false; _restoreRetryAgain = false; }" in retry
    # The drain is bounded, and re-checks the generation on EVERY record: a
    # factory can synchronously tear the view down mid-loop, and finishing an
    # old generation's records after teardownView cleared `windows` would strand
    # them behind the inactive overlay.
    assert "for (let round = 0; round < 8; round++) {" in retry
    assert "if (_deactivated || _viewEpoch !== epoch) return;" in retry
    # A throw from outside the builder (a registry lookup) must put the id back,
    # or delete-before-build becomes delete-and-lose.
    assert "pending.ids.add(appId);" in retry


def test_restore_retry_is_hooked_outside_loadmods():
    # Constraint 5: the retry must survive mods_enabled:false (loadMods returns
    # EARLY, before it inits anything) and a loader throw, or a mods-off browser
    # loses restore entirely -- strictly worse than the bug being fixed. So it
    # hangs off loadMods()'s settlement in 90, not off its tail in 86.
    boot = (BROKER_DIR / "90_js_mod_boot.js").read_text(encoding="utf-8")
    assert "loadMods().then(" in boot
    assert boot.count("restoreAppWindowsAfterMods()") == 2,         "both the fulfil AND the reject handler must run the retry"
    assert ").catch(function (e) {" in boot,         "a throw from the reject handler would otherwise be an unhandled rejection"
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    body = loader[loader.index("async function loadMods"):
                  loader.index("function _applyPolicyLive")]
    assert "restoreAppWindowsAfterMods" not in body,         "loadMods's own body would skip the retry on its mods_enabled=false return"
    # And the rejected design stays rejected: boot must not wait on the loader.
    alv = (BROKER_DIR / "84_js_active_view_lifecycle.js").read_text(encoding="utf-8")
    ba = alv[alv.index("async function bootActiveView"):alv.index("function teardownView")]
    for banned in ("loadMods", "localInfo", "__mods"):
        assert banned not in ba,             f"bootActiveView must not put mod readiness ({banned}) on the restore path"


def test_mid_session_mod_enable_restores_its_windows():
    # #167 scope call: a mid-session bring-up is a deferred boot in all but name
    # (#157's post-login _applyPolicyLive brings pinned mods up with NO reload),
    # so it must run the same retry -- otherwise the "re-enabling its mod restores
    # it faithfully" promise buildAppWindow's null return is documented on is only
    # true across a page reload. Hooked at the two CALLERS of _bringUp, not inside
    # it: _applyPolicyLive calls _bringUp in a loop.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    # #182 widened the signature to _bringUp(decl, opts) -- match the name, not
    # the parameter list, so a later parameter does not read as a missing hook.
    bring = loader[loader.index("function _bringUp(decl"):
                   loader.index("function _takeDown(id)")]
    assert "restoreAppWindowsAfterMods" not in bring,         "_bringUp itself must not restore -- _applyPolicyLive calls it N times"
    setter = loader[loader.index("function setModEnabled"):
                    loader.index("// The boot entry")]
    assert "restoreAppWindowsAfterMods();" in setter
    assert "_takeDown(id);" in setter and setter.index("_bringUp(decl, { byUser: true });") <         setter.index("restoreAppWindowsAfterMods();")
    policy = loader[loader.index("function _applyPolicyLive"):
                    loader.index("async function notifyModsHostAuth")]
    assert "if (broughtUp) restoreAppWindowsAfterMods();" in policy
    assert policy.index("_bringUp(m);") < policy.index("if (broughtUp)"),         "the retry must run AFTER the whole bring-up cascade"


def test_restore_ordering_comments_are_not_backwards():
    # Two comments asserted the ordering as settled fact, in the direction that is
    # less likely -- and a false invariant in a comment is how the next person
    # builds on it. Both must now describe it as the race it is (#167).
    sticky = (BROKER_DIR / "mods/sticky/sticky.js").read_text(encoding="utf-8")
    assert "restoreAppWindows runs BEFORE loadMods" not in sticky
    assert "#167" in sticky
    # The store's fallback comment blamed "mods off" alone; mods being SLOW has
    # the identical effect, and scratchpad has the identical exposure to
    # file-manager. Both must be named.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    fallback = s54[s54.index("function buildAppWindow"):s54.index("migratePrefKeys")]
    assert "scratchpad" in fallback
    assert "not exist YET" in fallback
    # The null return is what the retry reads -- it must stay a null.
    assert "if (ak && ak !== 'sticky-note' && ak !== 'text-editor') return null;" in s54


def test_custom_restore_hook_contract_is_documented():
    # Constraint 7: a kind's own `restore` is called DIRECTLY, so it never gets
    # openAppWindow's dedup-by-id. The retry is scoped to unregistered kinds (which
    # by definition have no hook), so the hook keeps its once-per-generation
    # contract -- but a lease-loss rebuild IS a new generation.
    s54 = (BROKER_DIR / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    spec = s54[s54.index("// ---- window-kind registry"):s54.index("function _windowKindRegistry")]
    assert "dedup-by-id" in spec and "rebuildView" in spec
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "windows.get(rec.id)" in loader


# --------------------------------------------------------------------------- #
# ctx.theme -- the live theme + a change channel (#169)
# --------------------------------------------------------------------------- #

def _loader_src():
    return (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")


def _text_src():
    # #168's free-text primitive rides its own fragment: the loader passed the
    # 2500-line per-fragment cap, same split 62 got. Same <script>, same scope.
    return (BROKER_DIR / "86a_js_mod_settings_text.js").read_text(
        encoding="utf-8")


def _packages_src():
    # #163's runtime-installed package machinery: the topological sort, the
    # <script src="/mods/<id>/<gen>/<file>"> loader, late registration and the
    # union status model. Its own fragment for the same 2500-line cap reason.
    return (BROKER_DIR / "86b_js_mod_packages.js").read_text(encoding="utf-8")


def _ctx_ext_src():
    # #194's ctx-extension fragment: where a NEW ctx family is declared and
    # pushed into the loader's extender registry, because 86 is at the cap.
    return (BROKER_DIR / "86c_js_mod_ctx_ext.js").read_text(encoding="utf-8")


def _help_cards_src():
    # #78/S5's help-card sanitizer + ctx.registerHelpCards registry. Moved out
    # of the loader VERBATIM by #194 for the same 2500-line cap reason 86a and
    # 86b were split out; same <script>, same scope.
    return (BROKER_DIR / "86d_js_mod_help_cards.js").read_text(encoding="utf-8")


def _frag_fn(src, sig):
    """The body of a top-level function, by its 8-space-indented signature.
    Every declaration in these fragments sits at that indent and closes on a
    bare 8-space '}', so the first one after the signature ends it."""
    start = src.index("\n        " + sig)
    return src[start:src.index("\n        }\n", start)]


def _frag_nested_fn(src, sig, indent=12):
    """``_frag_fn`` for a closure one level deeper -- the Mods pane's
    _reflectManager / _rebuildRows live inside _mountModsManagerPane."""
    pad = "\n" + " " * indent
    start = src.index(pad + sig)
    return src[start:src.index(pad + "}\n", start)]


def _loader_fn(sig):
    return _frag_fn(_loader_src(), sig)


def test_theme_ctx_api_present_in_loader():
    # #169: a mod can read the live theme and subscribe to changes. Three calls,
    # no more: get() is the cheap {name, dark}, vars() is the deliberately
    # separate resolved-var read, onChange() is the subscription.
    loader = _loader_src()
    for sym in ("function _themeState(", "function _themeVars(",
                "function _themeBgHex(", "function _themeAsHex(",
                "function _themeSig(", "function _modThemeObserve(rec, fn)",
                "function notifyModTheme(", "function _fireThemeSubs(subs)"):
        assert sym in loader, f"missing #169 symbol: {sym!r}"
    ctx = loader[loader.index("                theme: {"):]
    ctx = ctx[:ctx.index("\n                },")]
    assert "get: function () { return _themeState(); }" in ctx
    assert "vars: function () { return _themeVars(); }" in ctx
    assert "onChange: function (fn) { return _modThemeObserve(rec, fn); }" in ctx
    # Additive: the ctx contract version does NOT move for a new family.
    assert "ctxVersion: 1," in loader
    # And it reaches the served page.
    assert "function notifyModTheme(" in INDEX_HTML
    assert "onChange: function (fn) { return _modThemeObserve(rec, fn); }" in INDEX_HTML


def test_theme_state_derives_from_the_live_dom_not_the_stored_key():
    # The whole point of #169's derivation: core stopped owning `theme` (#75), so
    # the synced key is the theme MOD's key and says nothing when that mod is
    # disabled/absent/replaced. `dark` therefore comes from a YIQ test on the
    # LIVE --bg (core's hoisted isDarkAccent, 65 -- NOT a second luminance
    # implementation), and `name` is only read out of the settings blob when an
    # INLINE --bg proves some theme mod actually applied one.
    body = _loader_fn("function _themeState(")
    assert "root.style.getPropertyValue('--bg')" in body, \
        "the INLINE var is the proof a theme was applied"
    assert body.index("root.style.getPropertyValue('--bg')") < \
        body.index("getSettings().theme"), \
        "the stored key must be read only INSIDE the inline-var proof"
    assert "isDarkAccent(hex)" in body, "dark must ride core's hoisted YIQ test"
    assert "let name = 'night';" in body and "let dark = true;" in body, \
        "the fallback is the shipped :root default, which #75 pins to night"
    # Nothing unparsed may reach normalizeHex: it answers PALETTE[0] (a blue) for
    # anything it cannot read, which would be a plausible-looking WRONG answer.
    hexer = _loader_fn("function _themeAsHex(")
    assert "return ''" in hexer


def test_theme_vars_is_a_separate_call_carrying_the_public_contract():
    # The issue's cost constraint: resolving the vars is one getPropertyValue per
    # property, and a subscriber that only branches on `dark` must never pay it.
    # So vars() is its own call and _themeState never builds the list.
    body = _loader_fn("function _themeVars(")
    for var in ("'--bg'", "'--bg-2'", "'--bg-3'", "'--fg'", "'--fg-dim'",
                "'--accent-default'", "'--sel-bg'",
                "'--ok'", "'--warn'", "'--danger'"):
        assert var in body, f"the public var contract must carry {var}"
    # Per-WINDOW and geometry vars are deliberately NOT part of it (the global
    # accent fallback --accent-default, asserted above, is).
    for var in ("'--accent'", "'--taskbar-h'", "'--title-h'",
                "'--handle-thick'", "'--corner-size'"):
        assert var not in body, f"{var} is not a global theme var"
    assert "PUBLIC_VARS" not in _loader_fn("function _themeState("), \
        "get() must not pay for the var list"
    # TDZ: the list is function-local, not a fragment-level const a hoisted
    # function could read before this fragment's top level has run.
    loader = _loader_src()
    assert "\n        const PUBLIC_VARS" not in loader
    assert "            const PUBLIC_VARS = [" in body


def test_theme_change_is_announced_from_every_mover():
    # Four movers can change what is on screen, and the local one is the whole
    # reason this can't just ride the /state convergence: _valueAccessor.set
    # writes the blob and calls the mod's onChange DIRECTLY, never through
    # applyThemeSettings. The fifth is boot itself (see the loadMods case below).
    for sig in ("function _valueAccessor(", "function notifyModSettings(",
                "function setModEnabled(", "function _applyPolicyLive(",
                "async function loadMods("):
        assert "notifyModTheme();" in _loader_fn(sig), \
            f"{sig} must announce a possible theme change"
    # In the local-pick path the announcement must come AFTER the mod's onChange
    # -- that is where the theme mod writes the vars notifyModTheme reads back,
    # so a subscriber can never be handed the pre-change DOM.
    setter = _loader_fn("function _valueAccessor(")
    assert setter.index("entry.onChange(value)") < setter.index("notifyModTheme();")
    # In the convergence path it must come ONCE, after the whole loop: every
    # control has converged by then, and one pull is one announcement.
    conv = _loader_fn("function notifyModSettings(")
    assert conv.count("notifyModTheme();") == 1
    assert conv.index("t.onChange(cur)") < conv.index("notifyModTheme();")
    # Boot: the theme mod is FIRST in _MODS today, so every subscriber inits
    # after the vars exist -- but that is an ORDERING accident, and a reorder
    # would otherwise strand a subscriber on the pre-theme answer forever.
    assert ui._MODS[0] == "mods/theme/theme.js"
    boot = _loader_fn("async function loadMods(")
    assert boot.index("initMod(decl);") < boot.index("notifyModTheme();")


def test_theme_subscribers_are_torn_down_with_their_mod():
    # Hygiene 1 -- no leak: the unsubscribe goes on rec.unloads, exactly like
    # _modClipboardObserve / _modAddStatusItem, so a disable, a #121 dependency
    # cascade, or an initMod rollback drops the subscriber.
    obs = _loader_fn("function _modThemeObserve(rec, fn)")
    assert "rec.unloads.push(off)" in obs
    assert "if (typeof fn !== 'function') return function () {};" in obs
    # Idempotent both ways: unsubscribing by hand and then tearing down must not
    # remove a SIBLING that happens to sit at the recycled index.
    assert "const i = subs.indexOf(sub);" in obs and "if (i !== -1)" in obs
    # Boxed entries, so the same fn registered twice keeps two identities, and
    # the box carries `rec` so a fire can tell a mod that is MID-teardown from
    # one that is merely still listed.
    assert "const sub = { fn: fn, rec: rec };" in obs
    # Not replayed on register -- which is only safe because the change detector
    # is primed on the 0 -> 1 transition.
    assert "if (!subs.length) {" in obs and "notifyModTheme._last" in obs


def test_theme_notify_is_isolated_reentrant_safe_and_free_when_unused():
    notify = _loader_fn("function notifyModTheme(")
    # Free until a mod opts in: notifyModSettings runs on EVERY /state
    # convergence, so the no-subscriber path must return before reading a style.
    assert notify.index("if (!subs || !subs.length) return;") < \
        notify.index("_fireThemeSubs(subs)")
    assert "_themeState()" not in notify
    # Hygiene 3 -- re-entrancy: a subscriber may change a setting from its
    # handler, which re-enters through _valueAccessor.set. Coalesce + replay,
    # BOUNDED so two subscribers fighting cannot livelock the UI thread.
    assert "notifyModTheme._firing" in notify and "notifyModTheme._pending" in notify
    assert "for (let pass = 0; pass < 4; pass++)" in notify
    assert "} finally {" in notify, "the firing flag must be released on a throw"
    assert "console.warn" in notify
    # The pre-loader call (85_js_startup -> notifyModSettings, before this
    # fragment's top level runs) stays a clean no-op.
    assert "if (!window.__mods) return;" in notify
    # Hygiene 2 -- one bad subscriber breaks neither the others nor the mover.
    fire = _loader_fn("function _fireThemeSubs(subs)")
    assert "catch (e) {" in fire and "console.error" in fire
    # Snapshot + liveness re-check: a handler may unsubscribe or tear a mod down
    # mid-fire, and a torn-down mod must not be called after its teardown.
    assert "const list = subs.slice();" in fire
    assert "if (subs.indexOf(sub) === -1) continue;" in fire
    # ...nor may a mod be called while its OWN teardown is draining: the drain is
    # LIFO, so a later-registered unload that changes a setting re-enters here
    # while the earlier-registered observer is still listed.
    assert "if (sub.rec && sub.rec.unloading) continue;" in fire
    assert "rec.unloading = true;" in _loader_fn("function _runUnloads(rec)")
    # The one guarantee this channel makes is that a callback sees the theme that
    # is LIVE, so a pass that has been superseded (a subscriber moved the theme)
    # must abort rather than hand the rest of the list a stale payload.
    assert "if (notifyModTheme._pending) return;" in fire
    assert fire.index("if (notifyModTheme._pending) return;") < \
        fire.index("sub.fn({")
    # Change-detected, and each subscriber gets its OWN object.
    assert "if (sig === notifyModTheme._last) return;" in fire
    assert "{ name: state.name, dark: state.dark }" in fire


def test_theme_symbols_are_declared_exactly_once_in_the_assembled_page():
    # Every fragment AND every mod script shares one function-declaration scope
    # (ui.py concatenates them into a single <script>), so a mod that declared
    # `function _themeState()` would silently REPLACE the loader's across the
    # whole page -- including for calls that already ran. Python assertions are
    # the only place that can catch it, because UI JS never executes in CI.
    for sym in ("_themeState", "_themeVars", "_themeBgHex", "_themeAsHex",
                "_themeSig", "_modThemeObserve", "notifyModTheme",
                "_fireThemeSubs"):
        n = INDEX_HTML.count("function " + sym + "(")
        assert n == 1, f"{sym!r} is declared {n}x in the assembled page"


def test_theme_subscriber_state_is_tdz_proof():
    # The loader header's TDZ note: notifyModTheme rides notifyModSettings, which
    # EARLIER fragments call before fragment 86's top-level assignment runs. So
    # the subscriber list is a property of window.__mods (a plain read of a
    # not-yet-created object is `undefined`, which the guard handles) and the
    # change-detector state lives on the function object -- never a fragment
    # `let`/`const`, which would throw a ReferenceError CI can never catch.
    loader = _loader_src()
    assert "themeSubs: []," in loader
    for banned in ("\n        const _themeSubs", "\n        let _themeSubs",
                   "\n        let _themeLast", "\n        const THEME_"):
        assert banned not in loader, f"{banned.strip()!r} is a TDZ hazard here"
    assert "window.__mods.themeSubs" in loader


# --------------------------------------------------------------------------- #
# the portable-mod contract (#163 / design 4, lint from design 12)
# --------------------------------------------------------------------------- #

#: Comments, string/template bodies and regex literals blanked, in ONE place:
#: the broker's own ``modinstall.blank_js_literals``, which the install-time
#: capability lint (#193) runs on every package it validates. This lint used to
#: own the scanner; it now shares it, because two hand-rolled JS scanners means
#: two sets of blind spots and only one of them ever gets the bug report. Its
#: regex-vs-division heuristic, the keyword list that keeps ``return /}/`` from
#: blanking the rest of a file, and the "not a parser" caveat all live there.
def _js_blank_literals(src):
    from webterm.broker import modinstall
    return modinstall.blank_js_literals(src)


#: A top-level statement's SKELETON begins with one of these iff it is a
#: declaration. Declarations are fine at top level -- they define names, they do
#: not act. What the contract forbids is top-level code that RUNS.
_JS_DECL_HEAD = re.compile(r"^(?:async)?function|^class|^(?:const|let|var)")
_JS_DECL = re.compile(r"^(?:async)?function[A-Za-z_$]|^class[A-Za-z_$]"
                      r"|^(?:const|let|var)[A-Za-z_$]")
#: ... and the name it declares, so the cross-fragment lint below can ask who
#: else reaches it.
_JS_DECL_NAME = re.compile(r"^(?:async)?function([A-Za-z_$][\w$]*)"
                           r"|^class([A-Za-z_$][\w$]*)"
                           r"|^(?:const|let|var)([A-Za-z_$][\w$]*)")


def _js_top_level_statements(src):
    """The SKELETON of each top-level statement: its depth-0 characters only.

    ``function f(a) { ... }`` -> ``functionf``; ``const A = {x: 1};`` ->
    ``constA=``; ``registerMod({...});`` -> ``registerMod``; a bare
    ``doThing();`` -> ``doThing``. Everything inside brackets is skipped, so
    indentation and the body are irrelevant -- which is the point, since a
    shipped mod is indented to sit inside the assembled inline script.

    A statement ends at a depth-0 ``;``, or at the ``}`` that closes a
    ``function``/``class`` declaration (which needs no semicolon).

    KNOWN LIMIT, stated rather than discovered: an initializer's own call is not
    separated out, so ``const X = f();`` reads as a declaration. The lint is
    about top-level STATEMENTS, not about proving an initializer is pure."""
    depth, cur, out = 0, [], []

    def flush():
        s = "".join(cur)
        del cur[:]
        if s:
            out.append(s)

    for ch in _js_blank_literals(src):
        if ch in "([{":
            depth += 1
            continue
        if ch in ")]}":
            depth -= 1
            if depth == 0 and ch == "}" and _JS_DECL_HEAD.match("".join(cur)):
                flush()
            continue
        if depth == 0:
            if ch == ";":
                flush()
            elif not ch.isspace():
                cur.append(ch)
    flush()
    return out


def _shipped_mod_scripts():
    return sorted((BROKER_DIR / "mods").rglob("*.js"), key=lambda p: p.as_posix())


def _mod_rel(path):
    return path.relative_to(BROKER_DIR / "mods").as_posix()


def test_shipped_mods_carry_no_use_strict_and_no_literal_script_close():
    # Rules 2 and 4 of the portable-mod contract, and they hold for EVERY
    # shipped mod with no exceptions -- a mod that breaks either is broken in
    # one tree or the other, not merely non-portable:
    #   * a leading "use strict" is INERT mid-bundle (it is not the first
    #     directive of the assembled script) but becomes a real directive once
    #     the file is loaded on its own, so the same bytes would run under
    #     different semantics in the two trees;
    #   * a literal </script> ends the inline script early and is fatal in-tree
    #     while being harmless out of tree -- the sharpest possible version of
    #     "these are not the same environment".
    assert _shipped_mod_scripts(), "no shipped mod scripts found -- lint is inert"
    for path in _shipped_mod_scripts():
        src = path.read_text(encoding="utf-8")
        # The HTML parser matches a script end tag ASCII-CASE-INSENSITIVELY and
        # accepts tab / LF / FF / CR / space / "/" / ">" after the name, so
        # `</SCRIPT>`, `</script >` and `</script/` all terminate the inline
        # script just as `</script>` does. A plain substring test would miss
        # every one of them.
        m = re.search(r"</script[\t\n\f\r />]", src, re.IGNORECASE)
        assert not m, \
            f"{_mod_rel(path)} carries a literal {m.group(0)!r} script terminator"
        # A directive is a top-level STRING STATEMENT, so the statement scan is
        # what actually decides -- a textual line match misses
        # `"use strict"; // why` and `"use strict"/*why*/;`. That scan is the
        # rule-1 test below; here we only need the source-text sanity check.
        assert '"use strict"' not in src and "'use strict'" not in src, \
            f"{_mod_rel(path)} mentions a 'use strict' directive"


def test_portable_mod_lint_nothing_runs_at_top_level_but_registermod():
    # Rule 1: nothing at top level may RUN except the registerMod(...) call.
    # DECLARATIONS ARE FINE -- six shipped mods declare top-level consts and
    # functions and are correct to; a top-level function is exactly how a mod
    # publishes a builder. What must not happen is top-level code that ACTS:
    # spliced into the inline script it would run at parse time, and loaded as
    # its own <script src> it would run after /info, against a desktop that is
    # already up. Same bytes, different world.
    #
    # Every shipped mod script passes. This is a forward guard, not a known-gap
    # list. (#177's retired agent-docs is held to the same rule by
    # test_retired_agent_docs_would_still_load_if_copied_back, which this no
    # longer reaches — _shipped_mod_scripts() scans mods/, not mods-deprecated/.)
    offenders = {}
    for path in _shipped_mod_scripts():
        bad = [s for s in _js_top_level_statements(path.read_text(encoding="utf-8"))
               if s != "registerMod" and not _JS_DECL.match(s)]
        if bad:
            offenders[_mod_rel(path)] = bad[:4]
    assert not offenders, (
        f"top-level code that RUNS, outside registerMod(...): {offenders}. "
        "Move it into init(ctx) -- top level may only declare.")


#: Every place a SHIPPED mod's top-level name is reached from outside that mod.
#: This is rule 3 (no reliance on another fragment calling into this one's
#: top-level functions) and rule 5 (top-level names must not collide), and it is
#: the reason these mods are NOT republishable as installed mods today: a
#: shipped mod is spliced into the one inline script, where every fragment
#: shares one global lexical environment and every declaration is instantiated
#: before any statement runs. A separately-loaded <script src> publishes its
#: names only after /info returns, so a caller that runs earlier -- core's
#: restore path, a keybinding, a sibling mod's init -- would find `undefined`.
#:
#: A DRIFT GUARD BOTH WAYS: a new edge fails here, and so does a stale one, so
#: decoupling a mod forces this list to shrink. Not a rewrite request -- whether
#: to break any of these couplings is a separate decision.
_MOD_CROSS_FRAGMENT_CALL_INS = {
    ("applyPattern", "pattern", "mod:theme/theme.js"),
    # #194 moved the help-card family out of the loader, verbatim, to get 86
    # back under the fragment cap -- so these two edges now sit in 86d. The
    # coupling itself is unchanged: same code, same one <script>.
    ("findHelpWindow", "help", "core:86d_js_mod_help_cards.js"),
    ("loadCodeMirror", "editor", "mod:scratchpad/scratchpad.js"),
    # (#177 retired agent-docs, which took two edges with it: `editorFile`
    # (editor) reached in from mods/agent-docs/, and `openAgentDocsWindow`
    # (agent-docs) reached in from mods/editor/. The editor's call site survives
    # as a `typeof` guard, which owns no shipped name, so it is not an edge.)
    ("openNoteOrEditorWindow", "editor", "core:54_js_app_windows_store.js"),
    ("openNoteOrEditorWindow", "editor", "mod:sticky/sticky.js"),
    ("refreshHelpCorpus", "help", "core:86d_js_mod_help_cards.js"),
    ("toggleHelpWindow", "help", "core:78_js_keybindings.js"),
}


def test_portable_mod_lint_cross_fragment_call_ins_are_known():
    # Who owns which top-level name, then who else names it. Comments and
    # strings are blanked first, so a name merely MENTIONED in a comment (there
    # are several) is not an edge -- only real code is.
    owner = {}
    for path in _shipped_mod_scripts():
        mod = _mod_rel(path).split("/")[0]
        for stmt in _js_top_level_statements(path.read_text(encoding="utf-8")):
            m = _JS_DECL_NAME.match(stmt)
            if m:
                owner[next(g for g in m.groups() if g)] = mod
    assert "openNoteOrEditorWindow" in owner, "the name scan found nothing"
    sources = ([("core:" + p.name, p) for p in sorted(BROKER_DIR.glob("*.js"))]
               + [("mod:" + _mod_rel(p), p) for p in _shipped_mod_scripts()])
    blanked = {label: _js_blank_literals(p.read_text(encoding="utf-8"))
               for label, p in sources}
    found = set()
    for name, mod in owner.items():
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for label, text in blanked.items():
            if label.startswith("mod:") and label[4:].split("/")[0] == mod:
                continue
            if pat.search(text):
                found.add((name, mod, label))
    new = sorted(found - _MOD_CROSS_FRAGMENT_CALL_INS)
    assert not new, (
        f"new cross-fragment reaches into a mod's top-level name: {new}. That "
        "works only because shipped mods are spliced into one script -- it "
        "makes the owning mod unpublishable as an installed mod. Add it here "
        "deliberately, or route it through ctx.")
    stale = sorted(_MOD_CROSS_FRAGMENT_CALL_INS - found)
    assert not stale, (
        f"these couplings are gone: {stale}. Delete them from "
        "_MOD_CROSS_FRAGMENT_CALL_INS.")


def test_portable_mod_lint_actually_detects_a_violation():
    # The lint is a hand-rolled scanner, so it gets its own proof that it can
    # both fail and not false-positive: a "// const X" inside a string, or a
    # brace inside a regex literal, must not fool it.
    def top(src):
        return _js_top_level_statements(src)
    assert top("registerMod({ id: 'x' });\n") == ["registerMod"]
    assert top("registerMod({ s: '// doThing();' });\n") == ["registerMod"]
    assert top("registerMod({ r: /[{};]/g });\n") == ["registerMod"]
    assert top("/* doThing(); */\nregisterMod({});\n") == ["registerMod"]
    # A regex AFTER A KEYWORD. Read as division, its `}` would corrupt the
    # bracket depth and its closing `/` would start a phantom regex that blanks
    # everything after it -- so the lint would stop seeing the file.
    assert top("function f() { return /}/; }\nconst A = 1;\n") == [
        "functionf", "constA=1"]
    assert top("function f() { return 1 / x / y; }\nconst A = 1;\n") == [
        "functionf", "constA=1"]
    # Declarations are FINE and stay distinguishable from a call.
    assert top("const A = 1;\nfunction f() { g(); }\nregisterMod({});\n") == [
        "constA=1", "functionf", "registerMod"]
    assert all(_JS_DECL.match(s) for s in top("const A = { x: 1 };\n"))
    # ... and top-level code that RUNS is caught.
    for bad in ('"use strict";\n', "applyPattern();\n",
                "if (x) { boom(); }\n", "window.X = 1;\n"):
        stmts = top(bad)
        assert stmts and not all(s == "registerMod" or _JS_DECL.match(s)
                                 for s in stmts), f"lint missed {bad!r}"


# --------------------------------------------------------------------------- #
# shipped-mods capability declarations (#193's manifest `permissions`)
# --------------------------------------------------------------------------- #

def _undeclared_shipped_capabilities(mods_root):
    """Every shipped-mod capability violation under ``mods_root``, as
    ``{mod dir name: (permission, file, line, evidence)}``.

    The install-time lint's OWN public, file-shaped functions
    (``modinstall.capability_uses`` / ``first_undeclared_capability``, #193)
    run over every ``mods/<id>/mod.json`` + every ``.js`` its directory ships
    -- not just the manifest's ``entry`` script, since several shipped mods
    (editor/, update/) split helpers into extra ``.js`` files that still run
    in the assembled page. A manifest with no ``permissions`` key reads as
    ``[]`` here (a positive claim to use nothing), because a SHIPPED mod
    never gets modinstall's "written before the lint existed" grandfather --
    every shipped mod ships today, in this change. This is the mirror of
    ``validate_package``'s own lint call, never a second scanner."""
    from webterm.broker import modinstall
    offenders = {}
    for manifest_path in sorted(Path(mods_root).glob("*/mod.json")):
        mod_dir = manifest_path.parent
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = meta.get("permissions", [])
        assert isinstance(declared, list), \
            f"{mod_dir.name}: mod.json permissions must be a list"
        files = {p.name: p.read_text(encoding="utf-8")
                 for p in sorted(mod_dir.glob("*.js"))}
        offender = modinstall.first_undeclared_capability(files, declared)
        if offender is not None:
            offenders[mod_dir.name] = offender
    return offenders


def test_shipped_mods_permissions_are_truthful():
    # Every shipped mod's mod.json `permissions` must cover everything its
    # source actually uses -- the sweep the issue asks for, run over the REAL
    # mods/** tree. mod-sync in particular used to carry no `permissions` at
    # all while pushing another broker's mod policy over hostFetch (#193);
    # its corrected declaration (["egress", "remote-admin"]) is proven here
    # exactly like every other mod's, not asserted by name.
    offenders = _undeclared_shipped_capabilities(BROKER_DIR / "mods")
    assert not offenders, (
        f"shipped mod(s) use a capability their mod.json `permissions` "
        f"omits: {offenders}. Add the missing permission(s), or fix the "
        f"source if the use was accidental.")
    # ...and every shipped mod must actually CARRY the key, with names from
    # the vocabulary. The sweep above reads an absent key as `[]`, so without
    # this loop deleting the line from a zero-capability manifest stayed green
    # -- while the wire emitted `permissions: null` and the pane rendered that
    # shipped mod as "undeclared (pre-lint)", a sentence that is nonsense for
    # a mod living in this repo. Absence is the PRE-LINT signal; nothing that
    # ships here is pre-lint.
    from webterm.broker import modinstall
    missing, unknown = [], []
    for manifest in sorted((BROKER_DIR / "mods").glob("*/mod.json")):
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        if "permissions" not in meta:
            missing.append(manifest.parent.name)
            continue
        declared = meta["permissions"]
        if not isinstance(declared, list):
            unknown.append(f"{manifest.parent.name}: not a list")
            continue
        for entry in declared:
            if entry not in modinstall.PERMISSIONS:
                unknown.append(f"{manifest.parent.name}: {entry!r}")
    assert not missing, (
        f"shipped mod(s) with no `permissions` key: {missing}. A shipped mod "
        f"is never pre-lint -- declare [] to say it uses none.")
    assert not unknown, (
        f"shipped mod(s) declaring a name outside the vocabulary "
        f"{sorted(modinstall.PERMISSIONS)}: {unknown}")


def test_shipped_mods_permissions_sweep_detects_a_seeded_false_declaration(
        tmp_path):
    # Proof the sweep above has teeth: a fixture mod whose mod.json
    # UNDER-declares what its source uses -- exactly the regression shape
    # this exists to catch (someone adds a ctx.file call and forgets the
    # manifest) -- must turn _undeclared_shipped_capabilities RED. Run
    # against a synthetic mods_root so this proves the SWEEP's own wiring
    # (directory walk, absent-permissions-as-[], multi-.js-file handling),
    # not just the underlying modinstall functions (already proven in
    # tests/test_mod_install.py).
    md = tmp_path / "x-seeded"
    md.mkdir()
    js = ("function init(ctx) { ctx.file.read('x'); }\n"
          "registerMod({ id: 'x-seeded', init: init });\n")
    (md / "x-seeded.js").write_text(js, encoding="utf-8")

    def write_manifest(permissions):
        (md / "mod.json").write_text(json.dumps(
            {"id": "x-seeded", "entry": "x-seeded.js",
             "permissions": permissions}) + "\n", encoding="utf-8")

    # The false declaration: claims nothing, uses `file`. Caught, and the
    # offender names the very capability and file the real check would.
    write_manifest([])
    offenders = _undeclared_shipped_capabilities(tmp_path)
    assert "x-seeded" in offenders, "the sweep missed a seeded violation"
    permission, name, line, evidence = offenders["x-seeded"]
    assert permission == "file"
    assert name == "x-seeded.js"

    # The honest twin passes -- so the sweep is not simply refusing every
    # fixture mod regardless of its declaration.
    write_manifest(["file"])
    assert _undeclared_shipped_capabilities(tmp_path) == {}

    # An absent `permissions` key is the same as declaring nothing (no
    # shipped-mod grandfather), so it is caught too.
    (md / "mod.json").write_text(json.dumps(
        {"id": "x-seeded", "entry": "x-seeded.js"}) + "\n", encoding="utf-8")
    assert "x-seeded" in _undeclared_shipped_capabilities(tmp_path)


def test_only_a_click_marks_a_mod_enable_as_consent():
    # #182: a mod may take a real-world action on being ENABLED that it must not
    # take on merely being INITIALIZED. ctx.enabledByUser is what tells those
    # apart, and it is only honest if exactly one caller sets it. Every other
    # path into initMod -- boot, the post-login pin apply (_applyPolicyLive), a
    # dependency cascade, __test.run() -- is a mod coming up for reasons nobody
    # chose just now, and must arrive with the flag false.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "function initMod(decl, opts)" in loader
    assert "ctx.enabledByUser = !!(opts && opts.byUser);" in loader

    setter = loader[loader.index("function setModEnabled"):
                    loader.index("// The boot entry")]
    assert "_bringUp(decl, { byUser: true });" in setter, \
        "the Control Panel checkbox is the ONE caller that may claim consent"

    policy = loader[loader.index("function _applyPolicyLive"):
                    loader.index("async function notifyModsHostAuth")]
    assert "_bringUp(m);" in policy and "byUser" not in policy, \
        "a broker-side pin applied after login is not a human clicking"

    # The cascade a bring-up triggers must not inherit it either: enabling a mod
    # that drags a dependency up is a click aimed at the one mod in the
    # checkbox, and implication is not consent.
    bring = loader[loader.index("function _bringUp(decl"):
                   loader.index("function _takeDown(id)")]
    assert "initMod(m);" in bring, \
        "the dependency cascade must init dependencies WITHOUT opts"
    assert bring.count("opts") == 2, \
        f"_bringUp should only take opts and pass them to its own init, got {bring.count('opts')}"


def test_the_update_mod_only_opts_in_on_that_click():
    # The one consumer, and the assertion that keeps the seam load-bearing: a
    # page load must reach pollTick() WITHOUT going through offerConsent().
    mod = (BROKER_DIR / "mods/update/update.js").read_text(encoding="utf-8")
    assert "if (ctx.enabledByUser) {" in mod
    calls = mod.count("offerConsent()") - mod.count("function offerConsent()")
    assert calls == 1, \
        f"exactly one call site, guarded by the consent flag (got {calls})"
    tail = mod[mod.index("renderChip();\n                start();"):]
    assert tail.index("ctx.enabledByUser") < tail.index("offerConsent()"), \
        "offerConsent must be reachable only through the flag"
    # Nothing may post a revoke on its own -- see the mod's own comment on why
    # an automatic `false` would make two browsers fight over one broker's gate.
    # A5 extends the guard to all three gates and to the update-policy.js
    # companion (the pure half of the write lives there now): a revoke must
    # always be variable-built from a human click, never a hardcoded false.
    assert "check_enabled: !!want" in mod
    companion = (BROKER_DIR / "mods/update/update-policy.js").read_text(
        encoding="utf-8")
    for label, src in (("update.js", mod), ("update-policy.js", companion)):
        for literal in ("check_enabled: false", "apply_enabled: false",
                        "restart_enabled: false"):
            assert literal not in src, f"hardcoded revoke in {label}: {literal}"


# --------------------------------------------------------------------------- #
# visibility-aware polling: a hidden tab slows to HIDDEN_MULT, never stops
# --------------------------------------------------------------------------- #

def test_visibility_timers_present_and_control_keepalive_stays_plain():
    # The shared machinery -- onVisibility (the callback registry),
    # visibilityInterval (the slow-while-hidden setInterval wrapper) and the
    # mod-facing makeModVisibilityApi -- lives once in core, keyed off both
    # 'visibilitychange' and 'pageshow' (a bfcache restore fires the latter but
    # skips the former), and a hidden tick slows to HIDDEN_MULT rather than
    # stopping outright.
    s64 = (BROKER_DIR / "64_js_sessions_poll_control.js").read_text(encoding="utf-8")
    for sentinel in (
        "function onVisibility",
        "function visibilityInterval",
        "function makeModVisibilityApi",
        "HIDDEN_MULT = 10",
        "visibilitychange",
        "pageshow",
    ):
        assert sentinel in s64, f"missing visibility-timer sentinel: {sentinel!r}"
    # The single-active-browser control WS ping is a PLAIN setInterval, never a
    # visibilityInterval: it is what keeps the lease socket alive through an
    # idle proxy, and slowing it 10x in a hidden tab would let the socket time
    # out from under a tab that still holds the lease.
    assert "rec.pingTimer = setInterval" in s64


def test_refresh_taskbar_pollers_ride_the_visibility_interval():
    # refreshTaskbar has two independent callers on their own timers -- the
    # active-view lifecycle's slow poll (84) and the launch menu's fast poll
    # (76) -- and BOTH must go through visibilityInterval, not a plain
    # setInterval: either one left un-slowed would keep hammering every
    # configured host's /sessions at full cadence from a tab nobody is
    # looking at.
    for rel in ("84_js_active_view_lifecycle.js", "76_js_launch_fullscreen.js"):
        src = (BROKER_DIR / rel).read_text(encoding="utf-8")
        assert "setInterval(refreshTaskbar" not in src, \
            f"{rel} must not poll refreshTaskbar on a plain interval"
        assert "visibilityInterval(refreshTaskbar" in src, \
            f"{rel} must poll refreshTaskbar through visibilityInterval"


def test_mod_loader_exposes_visibility_api():
    # Every mod record gets its own ctx.visibility, built from its own rec --
    # so a mod's pausableInterval/onVisibility subscriptions ride rec.unloads
    # and die with the mod exactly like every other per-mod seam.
    loader = (BROKER_DIR / "86_js_mod_loader.js").read_text(encoding="utf-8")
    assert "visibility: makeModVisibilityApi(rec)," in loader


def test_update_mod_poll_ticks_visibility_aware_but_restart_never_does():
    mod = (BROKER_DIR / "mods/update/update.js").read_text(encoding="utf-8")
    # The 30-minute check poll feature-detects ctx.visibility (a runtime-
    # installed copy of this mod can run against an older core that never
    # offers it) and, when present, ticks through pausableInterval.
    assert "ctx.visibility.pausableInterval(pollTick, POLL_MS)" in mod
    # The restart-wait machinery (restartSleep onward) must stay entirely free
    # of the visibility timers: it is a short, bounded wait for a broker that
    # is mid-relaunch, not a background poll, and slowing it while the tab is
    # hidden would leave the tab that just clicked "restart" stuck watching a
    # spinner long after the wait should have given up.
    tail = mod[mod.index("function restartSleep"):]
    assert "visibility" not in tail, \
        "the restart-wait machinery must not adopt the visibility timers"


def test_feature_detected_mods_adopt_pausable_interval():
    # Same feature-detect shape as the update mod (ctx.visibility ? ... :
    # plain setInterval), in every other shipped mod that runs its own poll.
    #
    # git.js and task-manager.js don't inline the ternary at the timer site --
    # they route it through a local wrapper (`pausable` in git.js, named
    # `tmPausableInterval` in task-manager.js) that does the feature-detect
    # once, called from the real timer site further down the file. Pinning
    # only the word "pausableInterval" is too weak for that
    # shape: reverting the CALL SITE back to a plain setInterval leaves the
    # now-unused wrapper (and the pinned word) sitting in the file, so the
    # old word-only assert stayed green while the mod was silently
    # un-adopted -- and its teardown `.stop()` would throw on the raw
    # interval id. Pin the call site itself, per file, alongside the
    # ctx.visibility feature-detect, so a call-site revert fails even with
    # the wrapper left behind.
    for rel in ("mods/aistatus/aistatus.js", "mods/git/git.js",
                "mods/task-manager/task-manager.js", "mods/clock/clock.js"):
        src = (BROKER_DIR / rel).read_text(encoding="utf-8")
        assert "pausableInterval" in src, \
            f"{rel} must feature-detect ctx.visibility.pausableInterval"

    git = (BROKER_DIR / "mods/git/git.js").read_text(encoding="utf-8")
    # The refresh poll must call the wrapper -- not just define it.
    assert "gitTimer = pausable(" in git, \
        "git.js's refresh poll must call the pausable() wrapper at its timer site"
    assert "ctx.visibility" in git, \
        "git.js must feature-detect ctx.visibility"

    tm = (BROKER_DIR / "mods/task-manager/task-manager.js").read_text(encoding="utf-8")
    assert "const timer = tmPausableInterval(" in tm, \
        "task-manager.js's periodic refresh must call the tmPausableInterval() wrapper at its timer site"
    assert "ctx.visibility" in tm, \
        "task-manager.js must feature-detect ctx.visibility"

    aistatus = (BROKER_DIR / "mods/aistatus/aistatus.js").read_text(encoding="utf-8")
    # aistatus.js inlines the ternary at the timer site (no separate
    # wrapper), so pin both branches of it: the pausableInterval call and
    # the plain-setInterval fallback it degrades to on an older core.
    assert "ctx.visibility.pausableInterval(tick, ms)" in aistatus, \
        "aistatus.js's poll must call ctx.visibility.pausableInterval at its timer site"
    assert "const id = setInterval(tick, ms);" in aistatus, \
        "aistatus.js must keep the plain-setInterval fallback branch"

    clock = (BROKER_DIR / "mods/clock/clock.js").read_text(encoding="utf-8")
    assert "ctx.visibility.pausableInterval(render, 1000)" in clock, \
        "clock.js's render timer must call ctx.visibility.pausableInterval at its timer site"
    assert "ctx.visibility" in clock, \
        "clock.js must feature-detect ctx.visibility"


def test_launch_host_items_and_launch_profile_respect_hidden_hosts():
    s76 = (BROKER_DIR / "76_js_launch_fullscreen.js").read_text(encoding="utf-8")
    body = s76[s76.index("function launchHostItems"):
               s76.index("function buildLaunchMenuItems")]
    # Index-order assertion (like the file's other auth/down checks): a hidden
    # host is PARKED, not down, so its early-return must come AFTER the
    # auth/down check rather than folding into it -- the two states offer
    # different rows (a hidden host still offers its unhide-on-click broker
    # row; an auth/down host offers nothing).
    assert body.index("if (state === 'auth' || state === 'down') return items;") \
        < body.index("if (host.hidden) return items;")

    prof = s76[s76.index("async function launchProfile"):
               s76.index("function profilesFailedRecently")]
    # launchProfile re-reads the host and its state at CLICK time (a menu row
    # can outlive the state it was painted from) and refuses a hidden host the
    # same way it refuses auth/down, surfacing the refusal through the shared
    # info modal rather than dialing a parked or unreachable broker.
    assert "hostMenuState(pollStateFor" in prof
    assert "fresh.hidden" in prof
    assert "openInfoModal" in prof


# --------------------------------------------------------------------------- #
# admin credential class -- the core GUI prompt flows (#191)
# --------------------------------------------------------------------------- #

def _cp_src():
    return (BROKER_DIR / "81_js_control_panel.js").read_text(encoding="utf-8")


def _admin_block():
    """The whole #191 machinery in 86b: its section header up to the uninstall
    section that follows it. The never-persist assertions run over this slice
    so a storage call ANYWHERE in the new code path fails, not just inside one
    pinned function."""
    src = _packages_src()
    return src[src.index("// ---- admin credential class (#191)"):
               src.index("// ---- uninstall ---")]


def test_admin_machinery_present_and_served():
    block = _admin_block()
    for sym in ("function _adminHeld(", "function _adminRequiredFor(",
                "async function _localAdminInfo(",
                "function _adminTokenPrompt(", "async function _adminRefused(",
                "function _adminPost(", "async function adminGatedFetch("):
        assert sym in block, f"missing #191 symbol: {sym!r}"
    # The credential travels ONLY in the dedicated header -- never
    # Authorization (the page token's slot) and never a URL.
    assert "headers.set('X-Webterm-Admin', tok);" in block
    # And it all reaches the served page.
    assert "async function adminGatedFetch(" in INDEX_HTML
    assert "X-Webterm-Admin" in INDEX_HTML


def test_admin_detection_is_info_based_and_old_brokers_keep_todays_wire():
    # Feature-detect off the /info `admin` key the page already holds -- an
    # absent key or required !== true is "page-token wire", full stop.
    det = _frag_fn(_packages_src(), "function _adminRequiredFor(")
    assert "if (adminInfo.required !== true) return false;" in det
    assert "adminInfo.routes.indexOf(route) !== -1" in det
    # The local read rides localInfo()'s memoized boot fetch: no new request,
    # so detection can never become a probe.
    loc = _frag_fn(_packages_src(), "async function _localAdminInfo(")
    assert "await localInfo()" in loc and "hostFetch" not in loc
    # Non-enforcing branch: the detect comes FIRST and returns the caller's
    # opts through plain hostFetch untouched -- no prompt, no header, byte
    # identical to today.
    gate = _frag_fn(_packages_src(), "async function adminGatedFetch(")
    assert "if (!_adminRequiredFor(adminInfo, route))" in gate
    assert "const res0 = await hostFetch(host, route, opts);" in gate
    assert gate.index("if (!_adminRequiredFor(adminInfo, route))") \
        < gate.index("_adminTokenPrompt")
    # ...and the broker's OWN 403 admin_required is honoured as detection even
    # when the cached /info predates the operator enabling the realm: that
    # answer proves the realm exists now AND that nothing was written, so the
    # flow continues into the prompt instead of dead-ending on a refusal the
    # operator can only clear by guessing that a reload is needed.
    assert "if (!(await _adminRefused(res0))) {" in gate
    # A credential-bearing request never follows a redirect.
    post = _frag_fn(_packages_src(), "function _adminPost(")
    assert "o.redirect = 'error';" in post
    # A held credential is keyed by broker IDENTITY (id + url), never by the
    # id alone -- ids survive a re-point by design, so an id-only key would
    # hand a re-pointed host the token learned for the old machine.
    key = _frag_fn(_packages_src(), "function _adminKey(")
    assert "host.id + '|' + String(host.url || '')" in key


def test_admin_token_is_closure_held_and_never_persisted():
    block = _admin_block()
    # The session hold is a function-object memo inside the page's one closure
    # scope -- forgotten on reload by construction.
    assert "_adminHeld._m = Object.create(null);" in block
    # No storage API, no pref, no synced blob, no URL may see the credential.
    # Call-shaped pins (the comments name localStorage as the thing NOT used).
    for banned in ("localStorage.", "localStorage[", "sessionStorage",
                   "savePrefs(", "prefs._", "getSettings(", "putHostState",
                   "document.cookie", "history.replaceState", "_stateBlob",
                   "serverStore", "URLSearchParams", "console.log("):
        assert banned not in block, \
            f"#191 admin machinery must never touch {banned!r}"
    # The prompt input is a password field: the value must not be readable off
    # a shared or streamed screen.
    prompt = _frag_fn(_packages_src(), "function _adminTokenPrompt(")
    assert "input.type = 'password';" in prompt
    assert "input.autocomplete = 'off';" in prompt
    # Honest lifecycle words in the dialog itself.
    assert "'Held in this page until you reload — '" in prompt
    assert "+ 'never saved to this browser, never synced.'" in prompt


def test_admin_403_clears_the_held_token_and_reprompts_exactly_once():
    gate = _frag_fn(_packages_src(), "async function adminGatedFetch(")
    # First prompt (no held token), then the ONE refused re-prompt.
    assert gate.count("_adminTokenPrompt(host, act, false)") == 1
    assert gate.count("_adminTokenPrompt(host, act, true)") == 1
    # A refused held token is cleared before the re-prompt, and cleared again
    # when the second attempt is also refused -- by COMPARE-and-delete, so a
    # late refusal carrying an already-replaced token cannot evict the
    # credential the operator just typed for a concurrent act.
    assert gate.count("_adminForget(key, tok);") == 2
    forget = _frag_fn(_packages_src(), "function _adminForget(")
    assert "if (held[key] === tok) delete held[key];" in forget
    # Honest sentinels: cancelled = nothing sent; refused = a 403 landed.
    assert "return { res: null, aborted: 'cancelled' };" in gate
    assert gate.count("aborted: 'refused' }") == 2
    # Only a 403 whose body says admin_required means "wrong/missing admin
    # credential". A 401 stays the page-token realm and never opens the
    # prompt; clone() keeps the body readable for the caller.
    ref = _frag_fn(_packages_src(), "async function _adminRefused(")
    assert "if (!res || res.status !== 403) return false;" in ref
    assert "j.error === 'admin_required'" in ref
    assert "res.clone().json()" in ref


def test_admin_gate_wired_at_every_core_gated_route_post():
    pkg = _packages_src()
    # The Mods pane's uninstall and install both ride the gate against the
    # SERVING broker, with honest per-outcome words.
    un = _frag_fn(pkg, "async function _modUninstallPost(")
    assert "adminGatedFetch(localHost()," in un
    assert "'/mods/uninstall'" in un and "await _localAdminInfo()" in un
    assert "gated.aborted === 'cancelled'" in un
    assert "gated.aborted === 'refused'" in un
    inst = _frag_fn(pkg, "async function _modInstallRun(")
    assert "adminGatedFetch(localHost()," in inst
    assert "'/mods/install'" in inst and "await _localAdminInfo()" in inst
    assert "'install a mod'" in inst
    # The Control Panel's per-host pin editor: detection off the /info this
    # section already fetched (modCatalogCache), never a probe; the quiet
    # fan-out path (mod-sync's bulk apply) keeps today's header-less wire
    # until its own #191 migration.
    cp = _cp_src()
    pins = _frag_fn(cp, "async function saveModPins(host, set, opts) {")
    assert "r = await hostFetch(host, '/mods/policy', wire);" in pins
    assert "adminGatedFetch(host, '/mods/policy'," in pins
    assert "modCatalogCache.get(host.id)" in pins
    assert "error: 'admin_cancelled'" in pins
    assert "error: 'admin_required'" in pins
    assert pins.index("if (quiet)") < pins.index("adminGatedFetch")
    # The catalog fetch captures the peer's `admin` key beside update/restart,
    # e1ca8e6-style: a missing key reads as "old build", never as a refusal.
    cat = _frag_fn(cp, "async function fetchModCatalog(host) {")
    assert "rec.admin = (j.admin && typeof j.admin === 'object')" in cat
    assert "admin: null" in cat
    # No core fragment touches /mods/rescan today (grep-verified at #191
    # time); if a rescan control appears it must take the same gate, so its
    # mere mention here should send its author to adminGatedFetch.
    for frag in sorted(BROKER_DIR.glob("[58]*_js_*.js")):
        src = frag.read_text(encoding="utf-8")
        assert "/mods/rescan" not in src, \
            f"{frag.name} touches /mods/rescan -- route it through adminGatedFetch"


# --------------------------------------------------------------------------- #
# ctx.capabilities + declarative `needs` (#197)
# --------------------------------------------------------------------------- #

_NEEDS_SLICE_START = "// ---- ctx.capabilities + the `needs` gate (#197) ---"
_NEEDS_SLICE_END = "// ---- end ctx.capabilities + needs gate ---"


def _needs_source():
    """#197's range in 86c, verbatim. Declaration-only apart from the capability
    seeding loop and the guarded _registerCtxExtender call, which is what lets
    the node harness below run the SHIPPED code instead of a copy of it."""
    src = _ctx_ext_src()
    start = src.index(_NEEDS_SLICE_START)
    end = src.index(_NEEDS_SLICE_END)
    assert start < end, "slice markers out of order"
    body = src[start:end]
    for needed in ("const _MOD_CTX_CAPABILITIES = Object.create(null);",
                   "function _registerModCapability(name, level) {",
                   "function _modCtxHas(obj, path) {",
                   "function _modCapabilityMap(ctx) {",
                   "function _ctxCapabilities(ctx) {",
                   "function _modNeedsDecl(decl) {",
                   "function _modUnmetNeeds(decl, ctx) {",
                   "function _modNeedsGate(id, decl, ctx, rec) {"):
        assert needed in body, f"{needed} missing from the sliced range"
    return body


def _ctx_family_keys():
    """The TOP-LEVEL members of makeCtx's ctx literal -- the real v1 surface a
    mod is handed (a nested member sits two indents deeper and is not a
    family)."""
    src = _loader_src()
    start = src.index("            const ctx = {")
    end = src.index("\n            };\n", start)
    return re.findall(r"^ {16}('[^']+'|[A-Za-z_$][\w$]*)\s*:", src[start:end],
                      re.M)


def _ctx_families_added_by_extenders():
    """Top-level families an EXTENDER puts on the ctx: `ctx.<name> = ...` in a
    ctx-extension fragment.

    Found by the checkpoint-7 review: the seed gate below parsed only makeCtx's
    literal, and no future family will ever land there -- the cap forbids it,
    so every family after v1 arrives through an extender in 86c (or a later
    86*-ordered fragment). The gate therefore could not see the very families
    it claimed to police. `ctx.<a>.<b> = ...` is deliberately NOT a family: it
    decorates an existing one."""
    found = set()
    for frag in sorted(BROKER_DIR.glob("86[c-z]_js_*.js")):
        src = frag.read_text(encoding="utf-8")
        for name in re.findall(r"^\s*ctx\.([A-Za-z_$][\w$]*)\s*=(?!=)",
                               src, re.M):
            found.add(name)
    return found


def test_capability_seed_is_the_real_ctx_surface_minus_metadata():
    # #197: the map's whole value is that it is TRUE. Its seed is therefore the
    # ctx literal's own top-level members, minus `id`/`ctxVersion`, which are
    # metadata ABOUT the ctx rather than surface a mod can use. This drift gate
    # is what makes "every later family registers its entry" enforceable: add a
    # ctx member without a capability entry and this fails.
    ext = _ctx_ext_src()
    seed = ext[ext.index("for (const _cap of ["):]
    seed = seed[:seed.index("]")]
    seeded = set(re.findall(r"'([^']+)'", seed))
    families = set(_ctx_family_keys()) - {"id", "ctxVersion"}
    assert seeded == families, (
        f"capability seed drifted from makeCtx: "
        f"missing={sorted(families - seeded)} extra={sorted(seeded - families)}")
    # ...and every seeded name is a bare member name, never a dotted path: the
    # map is per FAMILY, and paths are what `needs` resolves.
    assert not [n for n in seeded if "." in n]
    # THE HALF THAT ACTUALLY BINDS THE FUTURE. Every family after v1 arrives
    # through an extender, not through the literal above, so scan the
    # extension fragments too: a `ctx.<family> = ...` with no capability entry
    # would leave `ctx.capabilities` lying by omission -- a mod would be told
    # it lacks a surface it demonstrably has, and #197's whole promise is that
    # the map is a true inventory.
    from_extenders = _ctx_families_added_by_extenders()
    unregistered = sorted(from_extenders - seeded)
    assert not unregistered, (
        f"ctx families added by an extender with no capability entry: "
        f"{unregistered}. Add each to the seed list in "
        f"86c_js_mod_ctx_ext.js, or the map claims they do not exist.")


def test_the_needs_gate_call_sites_in_the_loader_are_guarded():
    # #197 lands in 86c (the loader is at the #68 cap); 86 carries only the two
    # calls INTO it, both `typeof`-guarded -- absence of the companion is no
    # gate at all, which is exactly what an older loader does with the field
    # (#157: a new key is invisible to an old build, and never an error).
    loader = _loader_src()
    reg = _frag_fn(loader, "function registerMod(decl) {")
    assert "needs: (typeof _modNeedsDecl === 'function')" in reg
    assert "? _modNeedsDecl(decl) : []," in reg
    init = _frag_fn(loader, "function initMod(decl, opts) {")
    assert "const refused = (typeof _modNeedsGate === 'function')" in init
    assert "? _modNeedsGate(id, decl, ctx, rec) : null;" in init
    assert "if (refused) return refused;" in init
    # The gate reads the ctx init() would ACTUALLY get: after makeCtx, after the
    # extenders it applies, and after enabledByUser -- and before init runs.
    _order_in(init,
              "ctx = makeCtx(id, rec);",
              "ctx.enabledByUser = !!(opts && opts.byUser);",
              "const refused = (typeof _modNeedsGate === 'function')",
              "if (refused) return refused;",
              "decl.init(ctx);")
    # `requires` is still checked first, before a slot is claimed or a ctx built.
    assert init.index("reason: 'requires'") < init.index("ctx = makeCtx(id, rec);")
    # Additive: `needs` does not move the contract version (a bump would refuse
    # every mod that pins v1).
    assert "ctxVersion: 1," in loader
    # The surface is declared in the companion, not the loader...
    for sym in ("function _modNeedsGate(", "function _registerModCapability(",
                "function _modCtxHas("):
        assert sym not in loader, f"{sym!r} belongs in 86c, not the loader"
    # ...and all of it reaches the served page, exactly once.
    for sym in ("function _modNeedsGate(id, decl, ctx, rec) {",
                "function _modCtxHas(obj, path) {",
                "const _MOD_CTX_CAPABILITIES = Object.create(null);",
                "Object.defineProperty(ctx, 'capabilities', {",
                "needs: (typeof _modNeedsDecl === 'function')"):
        assert INDEX_HTML.count(sym) == 1, \
            f"#197 symbol missing/duplicated in the served page: {sym!r}"
    # The extender registration is guarded too, for a page assembled without
    # #194's registry -- a bare call would be a ReferenceError there.
    ext = _ctx_ext_src()
    assert "if (typeof _registerCtxExtender === 'function') {" in ext
    assert ext.index("if (typeof _registerCtxExtender === 'function') {") \
        < ext.index("_registerCtxExtender(_ctxCapabilities);")


def test_the_mods_pane_names_the_unmet_need():
    # #197: the operator-visible half. Same `blocked` state and the same pane
    # machinery as a requires-block, one new cause label that NAMES the surface.
    pkgs = _packages_src()
    row = _frag_fn(pkgs, "function _modStatusRow(id, catRow, decl) {")
    assert "const unmetNeeds = (_modBag('unmetNeeds')[id] || []).slice();" in row
    assert "label = 'blocked (needs ' + unmetNeeds.join(', ') + ')';" in row
    # After the dependency block, because initMod checks `requires` FIRST (before
    # it builds a ctx at all) -- the row must name the refusal that happened.
    assert row.index("label = 'needs: ' + missing.map(") \
        < row.index("} else if (unmetNeeds.length) {") \
        < row.index("state = 'failed';")
    # Both halves on the row: what was declared, and what was missing.
    assert "needs: decl ? (decl.needs || []).slice() : []," in row
    assert "unmetNeeds: unmetNeeds," in row
    # `needs` comes from the REGISTRATION only. A broker cannot report it: it is
    # a fact about the loader THIS page is running, not about the catalog.
    assert "catRow.needs" not in pkgs
    for sym in ("label = 'blocked (needs ' + unmetNeeds.join(', ') + ')';",
                "const unmetNeeds = (_modBag('unmetNeeds')[id] || []).slice();"):
        assert sym in INDEX_HTML, f"#197 pane symbol missing from the page: {sym!r}"


# #197 is BEHAVIOUR -- a refusal, a rollback, a frozen map, a row label -- so
# these cases execute the SHIPPED slices in node, the way the #194 registry
# cases do: the loader's registerMod/initMod, the ctx-extender registry, 86c's
# whole #197 range and 86b's status row, with only the browser surface they
# touch stubbed. `makeCtx` is the one deliberate fixture (the real one is 550
# lines of DOM/fetch closures), reduced to a ctx SHAPE.
_NEEDS_HARNESS = r"""
'use strict';
// ---- the browser surface these shipped slices touch, and nothing else -----
const infos = [];
const errors = [];
console.info = function () {
    infos.push(Array.prototype.map.call(arguments, String).join(' '));
};
console.error = function () {
    errors.push(Array.prototype.map.call(arguments, String).join(' '));
};
console.warn = console.error;

const window = { __mods: {
    ctxVersion: 1,
    registered: [],
    active: new Map(),
    policy: {},
    catalog: [],
    packages: Object.create(null),
    missingRequires: Object.create(null),
    cycleState: Object.create(null),
    sorted: false,
} };
const ENABLED = new Set();
function _currentPackageId() { return null; }
function _lateRegister() {}
// The shipped isModEnabled minus its localStorage half, so a PIN still wins
// exactly as it does in the page.
function isModEnabled(id) {
    const pin = _pin(id);
    if (pin !== null) return pin;
    return ENABLED.has(id);
}

__LOADER__

__REGISTRY__

__NEEDS__

__PACKAGES__

// makeCtx reduced to a SHAPE. Deliberately NOT every v1 family -- `session`,
// `serverStore`, `clipboard`, `taskbar` and `desktop` are absent here, which is
// how a case proves the capability map reports what THIS ctx carries rather
// than what the registry declares.
function makeCtx(modId, rec) {
    const ctx = {
        id: modId,
        ctxVersion: window.__mods.ctxVersion,
        onUnload: function (fn) {
            if (typeof fn === 'function') rec.unloads.push(fn);
        },
        storage: { get: function () { return null; } },
        file: { read: function () {}, write: function () {} },
        windows: { onTerminalCreate: function () {} },
        settings: { boolean: function () {} },
        theme: { get: function () {} },
        registerWindowKind: function () {},
    };
    _applyCtxExtenders(ctx, rec);
    return ctx;
}

// ---- driver -------------------------------------------------------------
const inits = [];
const teardowns = [];
const caps = {};
function decl(id, needs, extra) {
    const d = {
        id: id,
        init: function (ctx) {
            inits.push(id);
            caps[id] = ctx.capabilities
                ? Object.assign({}, ctx.capabilities) : null;
            ctx.onUnload(function () { teardowns.push(id); });
            if (extra && extra.init) extra.init(ctx);
        },
    };
    if (needs !== null && needs !== undefined) d.needs = needs;
    if (extra && extra.requires) d.requires = extra.requires;
    return d;
}
function register(d) {
    registerMod(d);
    const reg = window.__mods.registered;
    return reg[reg.length - 1];
}
function active() { return Array.from(window.__mods.active.keys()); }
function bag() { return Object.keys(window.__mods.unmetNeeds || {}); }
function row(id) {
    const entry = window.__mods.registered.find(
        function (m) { return m.id === id; }) || null;
    return _modStatusRow(id, null, entry);
}

const CASES = {};

// An unmet need: not init'd, nothing left claimed, and the row names it.
CASES.unmet = function () {
    // a driver extender, to prove the refusal drains rec.unloads too
    _registerCtxExtender(function _probe(ctx, rec) {
        rec.unloads.push(function () { teardowns.push('extender'); });
    });
    const entry = register(decl('gitish', ['windows.createAppWindow']));
    ENABLED.add('gitish');
    const res = initMod(entry);
    const r = row('gitish');
    return { res: res, inits: inits, teardowns: teardowns, active: active(),
             declared: entry.needs, state: r.state, label: r.label,
             rowNeeds: r.needs, rowUnmet: r.unmetNeeds, enabled: r.enabled,
             infos: infos };
};

// A met need -- dotted ones included -- activates normally.
CASES.met = function () {
    const entry = register(decl('ok',
        ['file', 'file.read', 'windows.onTerminalCreate']));
    ENABLED.add('ok');
    const res = initMod(entry);
    const r = row('ok');
    return { res: res, inits: inits, active: active(), state: r.state,
             label: r.label, bag: bag(), caps: caps.ok };
};

// Dotted-path resolution, against the ctx a mod is actually handed.
CASES.paths = function () {
    const rec = { id: 'probe', unloads: [] };
    const ctx = makeCtx('probe', rec);
    const table = {};
    ['file', 'file.read', 'windows.onTerminalCreate', 'windows.createAppWindow',
     'settings.boolean', 'theme.get', 'registerWindowKind', 'session',
     'toString', 'constructor', 'file.toString', 'file.read.call',
     '', '.file', 'file.', 'file..read', 'nope.deeper', 'storage.get',
     'capabilities'].forEach(function (p) { table[p] = _modCtxHas(ctx, p); });
    return { table: table };
};

// One mod's map cannot reach the next mod's, and a mod cannot edit its own.
CASES.capabilities_shape = function () {
    // Two properties changed AFTER #197's criteria were graded, in the CP7
    // review: the accessor is non-enumerable, and a map built while the
    // extender pass is still running is answered but never cached.
    let spreadSaw = null, keysSaw = null, early = null, late = null, same = null;
    // An extender that reads `capabilities` MID-PASS: it must get a truthful
    // map, and must not freeze the mod's map before later families land.
    function _peeker(ctx) { early = ctx.capabilities; }
    _registerCtxExtender(_peeker);
    function _lateFamily(ctx) { ctx.lateAdded = { ok: 1 }; }
    _registerCtxExtender(_lateFamily);
    _registerModCapability('lateAdded', 1);
    const m = register(decl('shape', null, { init: function (ctx) {
        spreadSaw = Object.prototype.hasOwnProperty.call({ ...ctx },
                                                         'capabilities');
        keysSaw = Object.keys(ctx).indexOf('capabilities') !== -1;
        late = ctx.capabilities;
        same = ctx.capabilities === late;      // now it caches
    } }));
    ENABLED.add('shape');
    initMod(m);
    return {
        // non-enumerable: an existing spread of ctx is byte-identical to before
        spreadSaw: spreadSaw, keysSaw: keysSaw,
        // the mid-pass read did NOT cache a partial map: init() sees the family
        // registered after the peeker ran
        earlyHadLate: !!(early && early.lateAdded),
        lateHasLate: !!(late && late.lateAdded),
        cachedAfterInit: same,
        frozenLate: Object.isFrozen(late),
    };
};

CASES.frozen = function () {
    let first = null, second = null, mutated = null, threw = 0;
    const a = register(decl('a', null, { init: function (ctx) {
        first = ctx.capabilities;
        try { ctx.capabilities.file = 99; } catch (e) { threw++; }
        try { ctx.capabilities.injected = 1; } catch (e) { threw++; }
        try { ctx.capabilities = { everything: 1 }; } catch (e) { threw++; }
        mutated = { file: ctx.capabilities.file,
                    injected: ctx.capabilities.injected === undefined };
    } }));
    const b = register(decl('b', null, { init: function (ctx) {
        second = ctx.capabilities;
    } }));
    ENABLED.add('a');
    ENABLED.add('b');
    initMod(a);
    initMod(b);
    return { firstKeys: Object.keys(first).sort(),
             secondKeys: Object.keys(second).sort(),
             frozen: Object.isFrozen(first), same: first === second,
             file: second.file, injected: second.injected === undefined,
             session: second.session === undefined, mutated: mutated,
             threw: threw, inits: inits };
};

// A family whose extender is registered AFTER this one still lands in the map:
// it is built on first READ (inside init), not while the extender runs. Plus
// the registrar's refusals.
CASES.late_family = function () {
    const added = [_registerModCapability('extraFamily', 3),
                   _registerModCapability('file', 2),
                   _registerModCapability('capabilities', 1),
                   _registerModCapability('not.an.identifier', 1),
                   _registerModCapability('', 1),
                   _registerModCapability(42, 1)];
    _registerCtxExtender(function _extraFamily(ctx) {
        ctx.extraFamily = { go: function () {} };
    });
    const entry = register(decl('late', ['extraFamily']));
    ENABLED.add('late');
    const res = initMod(entry);
    return { added: added, res: res, caps: caps.late };
};

// A pin is policy; a need is a fact about this build.
CASES.pinned = function () {
    window.__mods.policy = { pinned: true };
    const entry = register(decl('pinned', ['windows.createAppWindow']));
    const res = initMod(entry);
    const r = row('pinned');
    return { res: res, enabled: r.enabled, pin: r.pin, toggleable: r.toggleable,
             state: r.state, label: r.label, inits: inits, active: active() };
};

// initMod checks `requires` before it builds a ctx, so a mod with both is
// reported on the refusal that really happened.
CASES.requires_first = function () {
    const entry = register(decl('dependent', ['windows.createAppWindow'],
                                { requires: ['absent'] }));
    ENABLED.add('dependent');
    const res = initMod(entry);
    const r = row('dependent');
    return { res: res, state: r.state, label: r.label, rowUnmet: r.unmetNeeds,
             bag: bag() };
};

// A mod that comes up later must not keep reading blocked on stale news.
CASES.cleared = function () {
    const entry = register(decl('flip', ['windows.createAppWindow']));
    ENABLED.add('flip');
    const before = initMod(entry);
    const blocked = row('flip');
    entry.needs = ['file'];             // as a build that offers it would see
    const after = initMod(entry);
    const now = row('flip');
    return { before: before, after: after, blockedState: blocked.state,
             blockedLabel: blocked.label, state: now.state, label: now.label,
             bag: bag(), active: active() };
};

// The OLD-loader harness runs this one: the companion is not on the page, so
// there is no gate and no capability map -- and a needs-declaring mod must
// still register and run exactly as it does today.
CASES.old_loader = function () {
    const entry = register(decl('legacy', ['impossible.surface']));
    ENABLED.add('legacy');
    const res = initMod(entry);
    const r = row('legacy');
    return { res: res, declared: entry.needs, inits: inits, active: active(),
             caps: caps.legacy, state: r.state, label: r.label,
             rowNeeds: r.needs, rowUnmet: r.unmetNeeds, errors: errors };
};

const want = process.argv[2];
if (!CASES[want]) { console.log('no such case: ' + want); process.exit(2); }
const out = CASES[want]();
if (!('errors' in out)) out.errors = errors;
process.stdout.write(JSON.stringify(out) + '\n');
"""


def _needs_harness_text(with_companion=True):
    loader = _loader_src()
    pkgs = _packages_src()

    def whole(src, sig):
        # _frag_fn stops BEFORE the closing brace; a harness needs the whole
        # declaration back.
        return _frag_fn(src, sig) + "\n        }\n"

    loader_bits = "\n".join(whole(loader, sig) for sig in (
        "function ModConflictError(message) {",
        "function registerMod(decl) {",
        "function _runUnloads(rec) {",
        "function _pin(id) {",
        "function initMod(decl, opts) {"))
    pkg_bits = "\n".join(whole(pkgs, sig) for sig in (
        "function _modBag(name) {",
        "function _modIsRegistered(id) {",
        "function _modClamp(v, n) {",
        "function _modPermissionsView(catRow) {",
        "function _modStatusRow(id, catRow, decl) {"))
    return (_NEEDS_HARNESS
            .replace("__LOADER__", loader_bits)
            .replace("__REGISTRY__", _ctx_registry_source())
            .replace("__NEEDS__", _needs_source() if with_companion
                     else "// (the #197 companion is NOT on this page)")
            .replace("__PACKAGES__", pkg_bits))


@pytest.fixture(scope="module")
def needs_harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("needs")
    new = d / "harness.js"
    new.write_text(_needs_harness_text(True), encoding="utf-8")
    old = d / "harness_old.js"
    old.write_text(_needs_harness_text(False), encoding="utf-8")
    return {"new": new, "old": old}


def _run_needs(harness, case, which="new"):
    proc = subprocess.run([NODE, str(harness[which]), case],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"case {case} ({which}) failed (rc={proc.returncode})\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unmet_need_leaves_the_mod_uninitialised_and_says_why(needs_harness):
    r = _run_needs(needs_harness, "unmet")
    # Refused structurally -- never a throw, like every other initMod refusal.
    assert r["res"] == {"ok": False, "reason": "needs",
                        "needs": ["windows.createAppWindow"]}
    assert r["inits"] == [], "init() must not run for an unmet need"
    assert r["active"] == [], "the slot claimed before ctx construction is released"
    # The ctx extenders ran (the ctx was built) and their teardowns were drained
    # exactly once -- the same rollback a failed init gets.
    assert r["teardowns"] == ["extender"]
    # registerMod normalized the declaration...
    assert r["declared"] == ["windows.createAppWindow"]
    # ...and the pane row is a `blocked` row that NAMES the missing surface.
    assert r["enabled"] is True
    assert r["state"] == "blocked"
    assert r["label"] == "blocked (needs windows.createAppWindow)"
    assert r["rowNeeds"] == ["windows.createAppWindow"]
    assert r["rowUnmet"] == ["windows.createAppWindow"]
    # ...and it is logged, the way an inactive-dependency block is.
    assert any("gitish" in line and "windows.createAppWindow" in line
               for line in r["infos"])
    assert r["errors"] == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_met_need_activates_normally(needs_harness):
    r = _run_needs(needs_harness, "met")
    assert r["res"] == {"ok": True, "id": "ok"}
    assert r["inits"] == ["ok"]
    assert r["active"] == ["ok"]
    assert r["state"] == "active" and r["label"] == "active"
    assert r["bag"] == [], "nothing is stashed for the pane on the met path"
    # The mod was handed a capability map naming what this build really has.
    assert r["caps"]["file"] == 1
    assert "session" not in r["caps"], \
        "the map must report the ctx, not the registry's declarations"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dotted_needs_resolve_against_the_live_ctx(needs_harness):
    t = _run_needs(needs_harness, "paths")["table"]
    for met in ("file", "file.read", "windows.onTerminalCreate",
                "settings.boolean", "theme.get", "registerWindowKind",
                "storage.get", "capabilities"):
        assert t[met] is True, f"{met!r} is on this ctx and must resolve"
    for unmet in ("windows.createAppWindow", "session", "nope.deeper"):
        assert t[unmet] is False, f"{unmet!r} is absent and must not resolve"
    # Inherited members are NOT surface: `in` would make every mod "have"
    # toString, constructor and Function.prototype.call.
    for inherited in ("toString", "constructor", "file.toString",
                      "file.read.call"):
        assert t[inherited] is False, f"{inherited!r} is inherited, not declared"
    # Malformed paths are unmet, not exceptions.
    for junk in ("", ".file", "file.", "file..read"):
        assert t[junk] is False, f"{junk!r} must not resolve"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_each_mod_gets_its_own_frozen_capability_map(needs_harness):
    r = _run_needs(needs_harness, "frozen")
    assert r["inits"] == ["a", "b"]
    assert r["frozen"] is True
    assert r["same"] is False, "two mods must not share one map object"
    # The first mod's attempts to edit its map changed nothing -- not even its
    # own copy (frozen), let alone the next mod's.
    assert r["mutated"] == {"file": 1, "injected": True}
    # The harness runs strict, so the refusals THROW; the served page is sloppy,
    # where the same three writes silently no-op. Isolation is the property that
    # matters and it holds either way -- this only pins that none of them lands.
    assert r["threw"] == 3, "a frozen, getter-only property refuses all three"
    assert r["file"] == 1 and r["injected"] is True
    assert r["firstKeys"] == r["secondKeys"]
    # Observed, not promised: this build's ctx has no `session`, so neither map
    # claims one even though the registry knows the name.
    assert r["session"] is True
    assert "session" not in r["firstKeys"]
    assert "file" in r["firstKeys"] and "windows" in r["firstKeys"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_family_registered_after_this_one_still_reaches_the_map(needs_harness):
    r = _run_needs(needs_harness, "late_family")
    # Only the first registration is accepted: a duplicate would silently
    # re-level somebody else's family, `capabilities` would re-enter the map's
    # own getter, and the rest are not identifiers.
    assert r["added"] == [True, False, False, False, False, False]
    # The extender that installs it was registered AFTER _ctxCapabilities, and
    # the map is still right, because it is built on first read from init().
    assert r["res"] == {"ok": True, "id": "late"}
    assert r["caps"]["extraFamily"] == 3
    assert r["caps"]["file"] == 1, "the seeded families survive a late one"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_pin_cannot_override_an_unmet_need(needs_harness):
    r = _run_needs(needs_harness, "pinned")
    # Pinned ON by this broker -- and still refused, because a pin is policy and
    # a need is a fact about this build.
    assert r["pin"] is True and r["enabled"] is True
    assert r["res"]["reason"] == "needs"
    assert r["inits"] == [] and r["active"] == []
    assert r["state"] == "blocked"
    assert r["label"] == "blocked (needs windows.createAppWindow)"
    # ...and the checkbox stays locked, exactly as it is for any pinned row.
    assert r["toggleable"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_inactive_dependency_is_reported_before_an_unmet_need(needs_harness):
    r = _run_needs(needs_harness, "requires_first")
    # initMod checks `requires` before it builds a ctx, so the ROW must name
    # that refusal rather than a need the loader never got as far as testing.
    assert r["res"]["reason"] == "requires"
    assert r["state"] == "blocked"
    assert r["label"] == "needs: absent (not loaded)"
    assert r["rowUnmet"] == [] and r["bag"] == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_needs_block_is_cleared_when_the_mod_comes_up(needs_harness):
    r = _run_needs(needs_harness, "cleared")
    assert r["before"]["reason"] == "needs"
    assert r["blockedState"] == "blocked"
    assert r["blockedLabel"] == "blocked (needs windows.createAppWindow)"
    # Same mod, needs now met: the pane must not keep painting the old news.
    assert r["after"] == {"ok": True, "id": "flip"}
    assert r["state"] == "active" and r["label"] == "active"
    assert r["bag"] == [] and r["active"] == ["flip"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_loader_without_the_companion_still_loads_a_needs_mod(needs_harness):
    # #157 applied to this feature: a page assembled without 86c has no gate at
    # all, which is exactly what an OLDER loader does with an unknown decl field
    # -- registerMod copies fields selectively, so `needs` is simply dropped.
    # Absence is never an error, and it is never a refusal either.
    r = _run_needs(needs_harness, "old_loader", which="old")
    assert r["res"] == {"ok": True, "id": "legacy"}
    assert r["declared"] == [], "no companion -> the field is dropped, not kept"
    assert r["inits"] == ["legacy"] and r["active"] == ["legacy"]
    assert r["caps"] is None, "and there is no ctx.capabilities to read there"
    assert r["state"] == "active" and r["label"] == "active"
    assert r["rowNeeds"] == [] and r["rowUnmet"] == []
    assert r["errors"] == []


# ---- the registry survives its own adversaries (#194, CP7 review) ----------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_registering_during_the_pass_cannot_extend_the_running_loop(
        ctx_ext_harness):
    """Found by the checkpoint-7 adversarial pass: the loop iterated the LIVE
    array, so an extender appending a fresh function identity every call never
    terminated -- and ctx construction hanging takes the desktop with it. The
    pass now runs over a snapshot; a mid-pass registration applies from the
    NEXT mod on, which is also the only order anyone can reason about."""
    r = _run_ctx_ext(ctx_ext_harness, "registers_during_the_pass")
    assert r["terminated"] is True
    assert r["firstPass"] == ["greedy"], (
        "the extender registered mid-pass must not run in that same pass")
    assert r["secondPass"] == ["greedy", "later1"], (
        "...but it must take effect for the next mod")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_throwing_failure_report_does_not_cost_the_remaining_extenders(
        ctx_ext_harness):
    """The catch block reads `fn.name` and `ctx.id` to say what failed. Both
    are attacker-adjacent (a proxy, a throwing getter), and a throw THERE
    escaped the loop -- so one bad extender could silently cost every extender
    after it, which is the failure the isolation guarantee exists to prevent."""
    r = _run_ctx_ext(ctx_ext_harness, "throwing_report_surface")
    assert r["threw"] is False, "the report's own throw reached makeCtx's caller"
    assert r["log"] == ["bad", "after"], "the sibling after the thrower was skipped"
    assert r["siblingRan"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_ctx_capabilities_is_non_enumerable_and_never_caches_a_partial_map(
        needs_harness):
    """Both properties were added in the checkpoint-7 review and shipped
    untested until now.

    A READ is what freezes the map, and an enumerable accessor gets read by
    things that are not asking -- `{...ctx}`, `Object.assign`, a JSON
    round-trip, a dev-tools expansion. If any of those happened DURING the
    extender pass, the mod would be handed a map missing every family
    registered after that moment, and told for the rest of the page's life
    that it lacks a member it demonstrably has. Non-enumerable also keeps
    every pre-existing spread of `ctx` byte-identical to before the key
    existed."""
    r = _run_needs(needs_harness, "capabilities_shape")
    assert r["spreadSaw"] is False, "{...ctx} now carries (and freezes) the map"
    assert r["keysSaw"] is False, "Object.keys(ctx) exposes the accessor"
    # A mid-pass read is answered truthfully but not cached...
    assert r["earlyHadLate"] is False, (
        "the peeker read before the late family registered -- it cannot have "
        "seen it")
    # ...so init(), which runs after the pass, sees the complete inventory.
    assert r["lateHasLate"] is True, (
        "a partial map was cached mid-pass and the mod inherited it")
    assert r["cachedAfterInit"] is True and r["frozenLate"] is True
