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
from pathlib import Path, PurePosixPath

from webterm.broker import ui
from webterm.broker.ui import INDEX_HTML

BROKER_DIR = Path(ui.__file__).resolve().parent


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
    connect-ticket scheme, not a refactor -- see docs/TECHNICAL.md. This test
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


def test_assembled_equals_segment_join():
    # #71 splices the mod scripts (ui._MODS) into the one <script> BETWEEN the
    # loader and the boot fragment; #77 additionally splices each mod's manifest
    # .css into the head <style> zone, AFTER ui._MOD_CSS_AFTER and before
    # 40_body.html's </style>. So the served page is a 5-segment join, not a flat
    # join of _ORDERED. Rebuild it the same way ui.assemble does (mod-css comes
    # from the same best-effort ui._mod_css) and assert byte-equality with what
    # gets served. With no in-repo mod declaring `styles`, the css segment is
    # empty and this reduces to the #71 three-segment join.
    css_cut = ui._ORDERED.index(ui._MOD_CSS_AFTER) + 1
    js_cut = ui._ORDERED.index(ui._MOD_SPLICE_BEFORE)

    def _j(names):
        return "".join((BROKER_DIR / n).read_text(encoding="utf-8") for n in names)

    rebuilt = (
        _j(ui._ORDERED[:css_cut])
        + _j(ui._mod_css(ui._MODS, BROKER_DIR))
        + _j(ui._ORDERED[css_cut:js_cut])
        + _j(ui._MODS)
        + _j(ui._ORDERED[js_cut:])
    )
    assert rebuilt == INDEX_HTML


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
    from webterm.broker.app import _MODSTORE_ID_RE
    for m in ui.mod_catalog():
        assert _MODSTORE_ID_RE.fullmatch(m["id"]), \
            f"mod id {m['id']!r} cannot be used as a mod-policy key"


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
    # #104: the clock now owns a synced `clockTz` time-zone key through the new
    # searchable combo API (browser-global, def '' == follow the viewing
    # browser). The zone list is built dynamically from Intl.supportedValuesOf
    # with a curated fallback (Asia/Tokyo is one of the fallback markers). The
    # mod declares the `settings` tier on top of `taskbar` (order must match
    # _EXPECTED_TIERS).
    src = js.read_text(encoding="utf-8")
    for needle in ("registerMod(", "id: 'clock'", "ctxVersion: 1",
                   "tiers: ['taskbar', 'settings']",
                   "ctx.settings.combo('clockTz'", "def: ''",
                   "(browser default)", "Intl.supportedValuesOf", "Asia/Tokyo"):
        assert needle in src, f"missing clock-tz sentinel in mod src: {needle!r}"
    # And it ships in the served page — the mod script + the combo primitive it
    # relies on (the datalist-backed searchable input).
    for needle in ("ctx.settings.combo('clockTz'", "def: ''",
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
    # were split further into their own mods/agent-docs/ mod (requires the editor).
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
    # hoisted functions, so core (and the editor's legacy-upgrade branch) reach
    # them mods-off. The builder/loader/launcher ship in mods/editor/; the two
    # AGENTS.md openers ship in mods/agent-docs/ (#120) — both are concatenated
    # into the one shared <script>, so every symbol stays reachable.
    for sym in ("function openNoteOrEditorWindow", "function loadCodeMirror",
                "function openAgentDocsWindow", "function openAgentsMdEditor",
                "function launchTextEditor"):
        assert sym in INDEX_HTML, f"{sym!r} must stay reachable in the served page"
    # #120: the two AGENTS.md openers moved OUT of mods/editor/ and INTO
    # mods/agent-docs/ — assert they live in the agent-docs mod, not editor.js.
    editor_src = (BROKER_DIR / "mods" / "editor" / "editor.js").read_text(encoding="utf-8")
    agent_src = (BROKER_DIR / "mods" / "agent-docs" / "agent-docs.js").read_text(encoding="utf-8")
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


def test_agent_docs_mod_packaged_and_requires_editor():
    # #120: the Agent-docs feature (the tabbed AGENTS.md/CLAUDE.md editor opened
    # from the terminal 📋 button) is split into its own mod that REQUIRES the
    # editor mod. It reuses the editor's text-editor window kind (NO new appKind,
    # NO duplicate registerWindowKind, so webterm:appwindows:v1 stays byte-
    # identical) and inserts its 📋 title-bar button via the #116 per-terminal-
    # window seam (ctx.windows.onTerminalCreate) — core keeps zero Agent-docs knowledge.
    import json
    mod_dir = BROKER_DIR / "mods" / "agent-docs"
    js = mod_dir / "agent-docs.js"
    manifest = mod_dir / "mod.json"
    help_md = mod_dir / "help.md"
    assert js.is_file() and manifest.is_file() and help_md.is_file()
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    assert meta["id"] == "agent-docs"
    assert meta["ctxVersion"] == 1
    assert meta["entry"] == "agent-docs.js"
    assert meta["help"]["slug"] == "agent-docs"
    # Declared in _MODS, AFTER the editor it depends on (the #121 static ordering
    # guard also enforces this in test_requires_declared_before_dependency_...).
    assert "mods/agent-docs/agent-docs.js" in ui._MODS
    assert ui._MODS.index("mods/editor/editor.js") \
        < ui._MODS.index("mods/agent-docs/agent-docs.js")
    src = js.read_text(encoding="utf-8")
    assert "registerMod(" in src
    assert "id: 'agent-docs'" in src
    assert "ctxVersion: 1" in src
    # The hard dependency on the editor mod (the #121 requires primitive).
    assert "requires: ['editor']" in src
    # Rides the per-terminal-window seam and adds/tears-down its button there; NO
    # window kind of its own (it reuses the editor's text-editor kind).
    assert "ctx.windows.onTerminalCreate(" in src
    assert "registerWindowKind(" not in src, \
        "agent-docs must reuse the editor's text-editor kind, not register its own"
    assert "info.addTitleBarItem(" in src
    assert "info.onDispose(" in src
    # The 📋 button opens the editor keyed by the terminal WINDOW id (win.id) —
    # how openAgentsMdEditor keys sessions/windows — not the session wire id.
    assert "openAgentsMdEditor(win.id)" in src
    assert "btn-agentsmd" in src
    # Both moved openers live here now (and NOT in editor.js — see
    # test_editor_symbols_removed_from_core_fragments).
    assert "function openAgentDocsWindow" in src
    assert "function openAgentsMdEditor" in src
    # Ships in the served page, AFTER the editor mod, BEFORE the sticky mod.
    assert "id: 'agent-docs'" in INDEX_HTML
    assert INDEX_HTML.index("id: 'editor'") < INDEX_HTML.index("id: 'agent-docs'")
    assert INDEX_HTML.index("id: 'agent-docs'") < INDEX_HTML.index("id: 'sticky'")


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
    #    editor rather than a second way to set the same field.
    assert "saveModPins(" in code
    assert "'/mods/policy'" not in code, \
        "mod-sync must not POST /mods/policy itself -- it shares saveModPins"
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
    # ctxVersion is unchanged — ctx.serverStore is additive.
    assert "ctxVersion: 1" in loader
    # And it all reaches the served page.
    for sym in ("function _modStoreApi", "serverStore: {", "'/mod-store/'"):
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
        # setModEnabled cascades: a forward enable pass + a reverse disable pass.
        "regs.indexOf(decl) + 1",       # enable: init later deps-satisfied mods
        "const doomed = new Set([id]);",  # disable: transitive-dependent closure
        # the Mods pane reflects a dependency block READ-ONLY (needs: <ids>).
        "state = 'blocked'",
        "'needs: '",
        # the test API surfaces declared deps for the Playwright acceptance.
        "requires: (m.requires || []).slice()",
    ):
        assert sym in loader, f"missing #121 requires loader symbol: {sym!r}"
    # ctxVersion is unchanged — requires is additive plumbing.
    assert "ctxVersion: 1" in loader
    # And the key symbols reach the served page.
    for sym in ("requires: Array.isArray(decl.requires)",
                "reason: 'requires'",
                "state = 'blocked'",
                "requires: (m.requires || []).slice()"):
        assert sym in INDEX_HTML, \
            f"#121 requires symbol missing from served page: {sym!r}"


def test_requires_declared_before_dependency_in_mods_list():
    # #121: the static ordering guard that stands in for a runtime topological sort
    # + cycle detection. For every in-repo mod that declares requires:[ids] in its
    # registerMod, assert each listed id (i) is a KNOWN mod and (ii) is registered
    # STRICTLY EARLIER in ui._MODS. This makes cycles, self-require, and missing
    # dependencies unrepresentable, so boot's in-order loadMods loop is always
    # deps-first and the loader needs no runtime cycle detection. With no consumer
    # today this passes vacuously; it becomes load-bearing the moment #120 appends
    # agent-docs (requires: ['editor']) after mods/editor/editor.js.
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
            assert dep in id_to_index, (
                f"mod {mod_id!r} requires unknown mod id {dep!r} "
                f"(not a registrant in ui._MODS)")
            assert id_to_index[dep] < idx, (
                f"mod {mod_id!r} requires {dep!r} but it is not registered earlier "
                f"in ui._MODS (dep at index {id_to_index[dep]}, dependent at {idx})")


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
    "agent-docs": ["file", "window"],  # #120 AGENTS.md/CLAUDE.md openers do host /file/* I/O + open a window
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
        "function _mountModsManagerPane",
        "window.__mods.masterEnabled",   # master-gate state the live setter honors
        "set-mods-list",                  # the pane's list container class
    ):
        assert sym in loader, f"missing S13 loader symbol: {sym!r}"
    # The pane is built on the S1 pane scaffold (reuse, not a parallel renderer).
    assert "_modRegisterPane(rec, {" in loader
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
                    loader.index("function _mountModsManagerPane")]
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
    section = body[body.index('<div class="set-section" id="set-mod-policy">'):
                   body.index('id="set-mod-policy-hint"')]
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
    bring = loader[loader.index("function _bringUp(decl)"):
                   loader.index("function _takeDown(id)")]
    assert "restoreAppWindowsAfterMods" not in bring,         "_bringUp itself must not restore -- _applyPolicyLive calls it N times"
    setter = loader[loader.index("function setModEnabled"):
                    loader.index('// ---- "Mods" Control Panel pane')]
    assert "restoreAppWindowsAfterMods();" in setter
    assert "_takeDown(id);" in setter and setter.index("_bringUp(decl);") <         setter.index("restoreAppWindowsAfterMods();")
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


def _frag_fn(src, sig):
    """The body of a top-level function, by its 8-space-indented signature.
    Every declaration in these fragments sits at that indent and closes on a
    bare 8-space '}', so the first one after the signature ends it."""
    start = src.index("\n        " + sig)
    return src[start:src.index("\n        }\n", start)]


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
