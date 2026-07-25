"""Re-vendor the CodeMirror module graph from esm.sh (#146).

Run BY HAND, only when upgrading CodeMirror::

    python -m webterm.broker.vendor_codemirror          # fetch + rewrite + write
    python -m webterm.broker.vendor_codemirror --check  # verify what's committed

Not imported by the broker. ``httpx`` is imported inside :func:`main`, so the
runtime never needs it.

Why this exists at all
======================
Everything else third-party was vendored in #143, leaving ``https://esm.sh`` as
the last origin allowed to execute script in the origin that holds
``prefs._hosts[].token`` for EVERY configured host. It could not be SRI-pinned:
``mods/editor/codemirror.js`` imports ``@codemirror/{state,view,language}`` by
semver RANGE on purpose, because CodeMirror 6 splits across many packages that
must share ONE instance of each of those three, and an exact pin that drifts
behind what the transitive ranges resolve to loads a second instance whose facet
identities no longer match (for ``state`` that throws; for ``view`` it is silent
-- syntax highlighting simply stops). Ranges are mutable, so no hash could be
pinned, so the origin stayed trusted.

Vendoring moves that guarantee from "esm.sh dedupes correctly at request time"
to "there is exactly one copy of each build on disk, asserted here at generate
time". That assertion (:func:`_assert_single_instance`) IS the regression test
for the silent-highlighting bug.

What it fetches, and why BOTH layers are kept
=============================================
esm.sh serves two kinds of module and the graph interleaves them::

    # (1) a SHIM, at the range/entry URL -- what codemirror.js asks for
    GET /@codemirror/state@^6.5.0?target=es2022
        import "/@marijn/find-cluster-break@^1.0.0?target=es2022";
        export * from "/@codemirror/state@6.7.1/es2022/state.mjs";

    # (2) a CONCRETE build -- the real code
    GET /@codemirror/state@6.7.1/es2022/state.mjs
        import{findClusterBreak as $e}from"/@marijn/find-cluster-break@^1.0.0?target=es2022";

Concrete builds import the SHIM URLs, not each other. That shim layer is exactly
how esm.sh collapses many ranges (``^6.0.0``, ``^6.4.0``, ``^6.5.0`` ...) onto
one build, so it is kept verbatim rather than collapsed away: several shims may
point at one concrete file, which is what makes them one module instance. It
also means a shim's side-effect ``import`` lines are not silently dropped.

Nodes are keyed by their FINAL resolved URL, so two request URLs that redirect
to the same place become one local file -- i.e. one module instance, the
property this whole file exists to protect.

Layout
======
Flat directory, ``webterm/broker/vendor/codemirror/``. Not a mirrored tree: an
esm.sh key like ``/@codemirror/state@^6.5.0?target=es2022`` contains ``?`` and
``^``, which are not portable path components (``?`` is illegal on Windows). So
each module gets ``<slug>-<sha256[:10]>.mjs``; the slug is for humans and the
hash is what makes it unique. Every import specifier is rewritten to a relative
``./<name>.mjs``, which is why the directory must stay flat.

Two generated things are NOT upstream bytes:

``entry-<key>.mjs``
    One per key in ``CM_VER``, containing ``export * from "./<module>";``. This
    is the stable name ``codemirror.js`` imports, so an upgrade that changes
    every hashed filename does not touch ``codemirror.js``. ``export *`` is the
    same re-export form esm.sh's own shims use; it does not carry ``default``,
    so :func:`_assert_no_default_export` refuses to generate an alias for a
    module that has one.

``manifest.json``
    The served allowlist (``vendor.py`` reads it -- an explicit committed list,
    never a directory scan), plus each file's upstream URL and sha384 for
    ``tests/test_vendor_assets.py``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

_HERE = Path(__file__).resolve().parent

#: Where the vendored graph lands. Flat -- see the module docstring.
OUT_DIR = _HERE / "vendor" / "codemirror"

#: The loader whose CM_VER map is the single source of truth for what to fetch.
EDITOR_JS = _HERE / "mods" / "editor" / "codemirror.js"

MANIFEST = "manifest.json"

ORIGIN = "https://esm.sh"

JS_CTYPE = "application/javascript; charset=utf-8"

#: The three packages whose facet identities break if two builds load. `state`
#: throws ("multiple instances of @codemirror/state") -> textarea fallback;
#: `view` and `language` fail SILENTLY -- no highlighting, no error (#36).
SHARED_FACET_PACKAGES = ("@codemirror/state", "@codemirror/view",
                         "@codemirror/language")

#: A concrete esm.sh build: /<pkg>@<exact-version>/<target>/<file>.mjs
_CONCRETE_RE = re.compile(
    r"^/(?P<pkg>(?:@[^/@]+/)?[^/@]+)@(?P<ver>[^/^~*<>= ]+)/(?P<target>[^/]+)/")

#: One element of the module PROLOGUE: a comment, a directive, or a complete
#: static import/export-from statement. Applied repeatedly from offset 0, so it
#: only ever matches real statements -- never text that merely looks like one.
#:
#: Scanning the prologue instead of the whole file is not a shortcut, it is the
#: correctness requirement. `@codemirror/lang-javascript` ships completion
#: SNIPPETS whose payload is literal JavaScript source::
#:
#:     p('import {${names}} from "${module}"\n${}', {label:"import", ...})
#:
#: A file-wide `import ... from "..."` regex matches inside that string and a
#: rewrite would corrupt the snippet. ESM static imports are top-level-only and
#: esbuild (which builds esm.sh's output) hoists every one of them above the
#: first line of code, so the prologue is where they all are -- and
#: :func:`_assert_prologue_complete` fails loudly if that ever stops holding.
_PROLOGUE_RE = re.compile(
    r"""\s*(?:
          /\*.*?\*/                                       # block comment
        | //[^\n]*                                        # line comment
        | (?P<q0>["'])use\s+(?:strict|asm)(?P=q0)\s*;?     # directive prologue
        | import\s*(?P<q1>["'])(?P<spec1>[^"'\n]*)(?P=q1)\s*;?          # side effect
        | (?:import|export)(?![\w$])(?P<clause>[^"'`;]*?)\bfrom\s*
              (?P<q2>["'])(?P<spec2>[^"'\n]*)(?P=q2)\s*;?               # named / star
    )""", re.X | re.S)

#: The same two import forms, unanchored -- for the post-prologue pass in
#: :func:`_specifiers`, which supplies the statement-boundary check itself.
_IMPORT_RE = re.compile(
    r"""(?:import|export)(?![\w$])(?:
          \s*(?P<q1>["'])(?P<spec1>[^"'\n]*)(?P=q1)                     # side effect
        | (?P<clause>[^"'`;]*?)\bfrom\s*(?P<q2>["'])(?P<spec2>[^"'\n]*)(?P=q2)
    )""", re.X)

#: Syntax the rewriter does not understand. esm.sh emits none of it today; if a
#: future build does, fail loudly rather than ship a half-rewritten graph that
#: reaches for the network at runtime.
_UNSUPPORTED = (
    ("dynamic import()", re.compile(r"(?<![\w$.])import\s*\(")),
    ("import.meta", re.compile(r"(?<![\w$.])import\s*\.\s*meta")),
    ("import assertions", re.compile(r"\b(?:assert|with)\s*\{\s*type\s*:")),
)

#: A QUOTED absolute path or URL. After rewriting, one of these surviving means
#: a missed specifier -- i.e. "vendored" code that still fetches from the
#: network. Deliberately quote-anchored so the `/* esm.sh - <pkg>@<ver> */`
#: provenance header every upstream file carries is not mistaken for a leak.
#:
#: It covers any root-relative path ending in `.mjs`, not just
#: `/<pkg>@<version>/...`. An earlier version required the version and so said
#: nothing about esm.sh's node polyfills, which import bare `/node/events.mjs`
#: -- the graph shipped with two unrewritten specifiers and the editor silently
#: fell back to a textarea. Not "any root-relative path": `"/"` is an ordinary
#: string in a parser.
#: The absolute arm is pinned to esm.sh rather than matching any URL: view.mjs
#: contains "http://www.w3.org/2000/svg", which is a namespace, not an import.
_LEFTOVER_RE = re.compile(
    r"""["'](?:https?:)?//esm\.sh/[^"']*["']"""         # absolute / protocol-relative
    r"""|["']/[^"'\s]*\.mjs["']"""                      # /node/events.mjs
    r"""|["']/(?:@[^/"']+/)?[^/"']+@[^"']*["']""")      # /pkg@version/...


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

def _key_of(url: str) -> str:
    """Canonical key for a resolved esm.sh URL: its path plus query.

    The query is part of the identity -- ``?target=es2022`` selects a different
    build, and dropping it would collapse two distinct modules into one file.
    """
    parts = urlsplit(url)
    return parts.path + (("?" + parts.query) if parts.query else "")


def local_name(key: str) -> str:
    """``/@codemirror/state@6.7.1/es2022/state.mjs`` ->
    ``codemirror-state-6-7-1-es2022-state-<hash>.mjs``.

    The slug is lossy on purpose (readability); the sha256 of the FULL key is
    what guarantees uniqueness, including between ``@6.5.0`` and ``@^6.5.0``.
    Never longer than 96 chars, and only ``[a-z0-9.-]``: these are real path
    components on Windows too, where ``?``, ``^`` and ``:`` are illegal and a
    component may not exceed 255 bytes.
    """
    stem = key[:-4] if key.endswith(".mjs") else key
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    if len(slug) > 72:
        slug = slug[:72].rstrip("-")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}.mjs"


def entry_name(key: str) -> str:
    """Stable filename for a CM_VER entry point.

    The entry module is STORED under this name rather than aliased behind an
    `export * from` wrapper. A wrapper would have been observably lossy -- it
    never forwards `default`, and `import()` of it yields a different namespace
    object than `import()` of the target -- for no benefit, since renaming the
    node achieves the same thing: `codemirror.js` gets a stable path that
    survives an upgrade renaming every hashed file, and any other module that
    imports the same URL is rewritten to this same file, so it stays one
    instance.
    """
    return f"entry-{key}.mjs"


def _assign_names(node_keys, entries: Dict[str, str]) -> Dict[str, str]:
    """node key -> filename, with CM_VER entry points given stable names."""
    by_node: Dict[str, List[str]] = {}
    for cm_key, node in entries.items():
        by_node.setdefault(node, []).append(cm_key)
    clash = {n: ks for n, ks in by_node.items() if len(ks) > 1}
    if clash:
        raise SystemExit(
            "two CM_VER keys resolve to the same module, so they cannot both "
            "own its filename:\n" + "\n".join(
                f"  {n}: {', '.join(sorted(ks))}" for n, ks in clash.items()))

    names: Dict[str, str] = {}
    for node in node_keys:
        names[node] = (entry_name(by_node[node][0]) if node in by_node
                       else local_name(node))

    taken: Dict[str, str] = {}
    for node, name in sorted(names.items()):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.mjs", name) or len(name) > 128:
            raise SystemExit(f"generated filename is not portable: {name!r}")
        if name in taken:
            raise SystemExit(
                f"filename collision: {taken[name]} and {node} both map to "
                f"{name!r}")
        taken[name] = node
    return names


def sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_cm_ver(path: Path = EDITOR_JS) -> Dict[str, str]:
    """Parse the ``CM_VER`` object out of ``mods/editor/codemirror.js``.

    Parsed rather than duplicated so there is exactly one list of what the
    editor loads; a key added there is fetched here without a second edit.
    """
    src = path.read_text(encoding="utf-8")
    match = re.search(r"const\s+CM_VER\s*=\s*\{(.*?)\n\s*\};", src, re.S)
    if not match:
        raise SystemExit(f"could not find `const CM_VER = {{...}};` in {path}")
    body = re.sub(r"//[^\n]*", "", match.group(1))          # drop line comments
    out: Dict[str, str] = {}
    for key, spec in re.findall(r"(\w+)\s*:\s*'([^']+)'", body):
        out[key] = spec
    if not out:
        raise SystemExit(f"CM_VER in {path} parsed to zero entries")
    return out


# --------------------------------------------------------------------------
# fetch + rewrite
# --------------------------------------------------------------------------

def _scan_prologue(text: str) -> Tuple[List[Tuple[int, int, str]], int]:
    """Walk the module prologue from offset 0.

    Returns ``(specifiers, end)`` where each specifier is ``(start, end, spec)``
    spanning the quoted literal itself, and ``end`` is the offset of the first
    byte of real code.
    """
    specs: List[Tuple[int, int, str]] = []
    pos = 0
    while pos < len(text):
        match = _PROLOGUE_RE.match(text, pos)
        if not match or match.end() == pos:
            break
        for group in ("spec1", "spec2"):
            if match.group(group) is not None:
                specs.append((*match.span(group), match.group(group)))
        pos = match.end()
    return specs, pos


def _specifiers(text: str) -> List[Tuple[int, int, str]]:
    """Every static import/export specifier, left-to-right.

    Two passes, because neither alone is both safe and complete:

    * the PROLOGUE, matched exactly (comments, directives, whole import
      statements from offset 0). This is where esbuild puts imports, and
      matching it statement-by-statement means a string that merely looks like
      an import can never be mistaken for one.
    * anything AFTER the prologue that starts at a statement boundary -- the
      preceding non-space character is ``;``, ``}`` or ``{``. esm.sh's node
      polyfills need this: ``node/process.mjs`` declares two helper functions
      and only then imports ``/node/events.mjs``. ESM allows that (import
      declarations are top-level, not top-of-file) and a prologue-only scan
      shipped those two specifiers unrewritten.

    The boundary rule is what keeps the second pass off string contents:
    ``@codemirror/lang-javascript``'s completion snippets embed literal source
    (``p('import {${names}} from "${module}"…')``) where ``import`` follows a
    quote, not a statement terminator. Anything that slips through anyway fails
    loudly rather than silently -- a bogus specifier 404s during the crawl, and
    :data:`_LEFTOVER_RE` refuses to write a file with an unrewritten path.
    """
    specs, end = _scan_prologue(text)
    for match in _IMPORT_RE.finditer(text, end):
        pos = match.start() - 1
        while pos >= 0 and text[pos] in " \t\r\n":
            pos -= 1
        if pos < 0 or text[pos] not in ";}{":
            continue
        for group in ("spec1", "spec2"):
            if match.group(group) is not None:
                specs.append((*match.span(group), match.group(group)))
    return specs


def _assert_every_module_path_is_a_specifier(url: str, text: str) -> None:
    """Every module-shaped string in the file must be one the rewriter is about
    to rewrite.

    The self-check the first version lacked: it asserted only that no LEFTOVER
    pattern appeared past the prologue, which said nothing about a path shape
    the pattern did not describe. Comparing spans instead means an import the
    scanner cannot see is a loud failure here rather than a runtime fetch from
    a "vendored" file.
    """
    spans = {(start, end) for start, end, _ in _specifiers(text)}
    for hit in _LEFTOVER_RE.finditer(text):
        if (hit.start() + 1, hit.end() - 1) not in spans:      # strip the quotes
            raise SystemExit(
                f"{url}: {hit.group(0)[:80]} at offset {hit.start()} is not a "
                f"specifier the rewriter found, so it would ship unrewritten. "
                f"Teach _specifiers/_LEFTOVER_RE about this form.")


def _check_unsupported(url: str, text: str) -> None:
    for label, regex in _UNSUPPORTED:
        hit = regex.search(text)
        if hit:
            context = text[max(0, hit.start() - 60):hit.end() + 60]
            raise SystemExit(
                f"{url}: contains {label} at offset {hit.start()}, which this "
                f"generator cannot rewrite. Vendoring would ship code that "
                f"still reaches esm.sh at runtime. Teach the rewriter about it "
                f"before re-running.\n  ...{context}...")


#: Bounds on a run. Not tuning knobs -- they are what stops a mis-resolved or
#: hostile graph from crawling the internet or filling the disk. The real graph
#: is ~116 modules and ~1 MB.
MAX_NODES = 500
MAX_BYTES = 8 * 1024 * 1024


class Fetcher:
    """Fetches esm.sh URLs once each, keyed by FINAL (post-redirect) URL."""

    def __init__(self, client) -> None:
        self.client = client
        self.by_key: Dict[str, str] = {}                 # key -> source text
        self._resolved: Dict[str, str] = {}              # request url -> key
        self._bytes = 0

    def resolve(self, url: str) -> str:
        """Fetch ``url`` if new; return the canonical key of where it landed."""
        if url in self._resolved:
            return self._resolved[url]
        # Checked BEFORE the request, not after: this generator follows
        # specifiers out of third-party code, so an off-origin one must never
        # become a request at all.
        if not url.startswith(ORIGIN + "/"):
            raise SystemExit(f"refusing to fetch off-origin URL: {url}")
        if len(self._resolved) >= MAX_NODES:
            raise SystemExit(f"graph exceeded MAX_NODES={MAX_NODES}")
        response = self.client.get(url)
        if response.status_code != 200:
            raise SystemExit(f"{url}: HTTP {response.status_code}")
        ctype = response.headers.get("content-type", "")
        if "javascript" not in ctype:
            raise SystemExit(f"{url}: content-type {ctype!r}, expected JavaScript")
        final = str(response.url)
        if not final.startswith(ORIGIN + "/"):
            raise SystemExit(f"{url}: redirected off {ORIGIN} to {final}")
        if urlsplit(final).fragment:
            raise SystemExit(f"{final}: fragment in a module URL")
        self._bytes += len(response.content)
        if self._bytes > MAX_BYTES:
            raise SystemExit(f"graph exceeded MAX_BYTES={MAX_BYTES}")
        key = _key_of(final)
        self._resolved[url] = key
        if key not in self.by_key:
            # Strict UTF-8 from the raw bytes, NOT response.text: httpx would
            # fall back to a charset guess, and re-encoding a mis-decoded body
            # would silently change what we commit.
            try:
                text = response.content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SystemExit(f"{final}: not valid UTF-8 ({exc})")
            _check_unsupported(final, text)
            _assert_every_module_path_is_a_specifier(final, text)
            self.by_key[key] = text
        return key


def crawl(fetcher: Fetcher, cm_ver: Dict[str, str],
          log=print) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """BFS the graph from the CM_VER entry points.

    Returns ``(entry_key_by_cm_ver_key, imports)`` where ``imports`` maps a
    module key to ``{specifier as written: key it resolves to}``.
    """
    entries: Dict[str, str] = {}
    for name, spec in cm_ver.items():
        entries[name] = fetcher.resolve(ORIGIN + "/" + spec)
        log(f"  entry {name:<12} {entries[name]}")

    imports: Dict[str, Dict[str, str]] = {}
    queue: List[str] = list(dict.fromkeys(entries.values()))
    seen: Set[str] = set(queue)
    while queue:
        key = queue.pop(0)
        edges: Dict[str, str] = {}
        for _, _, spec in _specifiers(fetcher.by_key[key]):
            if spec.startswith("./") or spec.startswith("../"):
                raise SystemExit(
                    f"{key}: unexpected relative specifier {spec!r} -- esm.sh "
                    f"emits root-relative paths; the rewriter assumes that.")
            target = fetcher.resolve(urljoin(ORIGIN + key, spec))
            edges[spec] = target
            if target not in seen:
                seen.add(target)
                queue.append(target)
        imports[key] = edges
        log(f"  [{len(seen):>3}] {key}  ({len(edges)} imports)")
    return entries, imports


def rewrite(text: str, edges: Dict[str, str], names: Dict[str, str]) -> str:
    """Point every import specifier at its sibling file in this directory."""
    out: List[str] = []
    cursor = 0
    for start, end, spec in _specifiers(text):
        out.append(text[cursor:start])
        out.append("./" + names[edges[spec]])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------
# assertions -- the point of the exercise
# --------------------------------------------------------------------------

def concrete_builds(keys) -> Dict[str, Set[str]]:
    """package -> set of concrete versions present in the graph."""
    builds: Dict[str, Set[str]] = {}
    for key in keys:
        match = _CONCRETE_RE.match(key)
        if match:
            builds.setdefault(match.group("pkg"), set()).add(match.group("ver"))
    return builds


def _assert_single_instance(keys) -> Dict[str, str]:
    """Refuse to write a graph carrying two builds of a shared-facet package.

    This is the whole regression guard for #36: esm.sh used to enforce it at
    request time, and vendoring has to keep enforcing it or it quietly ships
    the exact bug the range-imports were protecting against.
    """
    builds = concrete_builds(keys)
    broken = {p: sorted(v) for p, v in builds.items() if len(v) > 1}
    if broken:
        raise SystemExit(
            "multiple concrete builds of the same package in the graph -- "
            "facet identities will not match:\n"
            + "\n".join(f"  {p}: {', '.join(v)}" for p, v in sorted(broken.items()))
            + "\n\nThis is what the range-vs-exact rules in codemirror.js are "
              "about. It happens when OUR exact pin for a package is older "
              "than what a transitive `^` range resolves to, so esm.sh serves "
              "both. Fix it in CM_VER, not here: change that package's pin "
              "from `@<exact>` to `@^<exact>` so it tracks the same build its "
              "transitive importers get.")
    for pkg in SHARED_FACET_PACKAGES:
        if pkg not in builds:
            raise SystemExit(f"no concrete build of {pkg} in the graph")
    return {p: next(iter(v)) for p, v in sorted(builds.items())}


def _assert_rewritten(name: str, text: str) -> None:
    hit = _LEFTOVER_RE.search(text)
    if hit:
        raise SystemExit(
            f"{name}: still references {hit.group(0)[:80]} after rewriting -- "
            f"a missed specifier means this 'vendored' file fetches from the "
            f"network at runtime.")


def _unreachable(entry_files: Set[str], modules, edges: Dict[str, List[str]]) -> Set[str]:
    """Files no entry point can reach: stale weight in the wheel, and a sign
    the stale-file sweep missed something an upgrade orphaned."""
    seen: Set[str] = set()
    queue = [f for f in entry_files if f in modules]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(edges.get(name, ()))
    return set(modules) - seen


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def build(fetcher: Fetcher, entries: Dict[str, str],
          imports: Dict[str, Dict[str, str]]) -> Dict[str, object]:
    """Rewrite every node and return the files plus the manifest."""
    versions = _assert_single_instance(imports)
    names = _assign_names(sorted(imports), entries)

    files: Dict[str, bytes] = {}
    modules: Dict[str, Dict[str, str]] = {}
    for key in sorted(imports):
        body = rewrite(fetcher.by_key[key], imports[key], names)
        name = names[key]
        _assert_rewritten(name, body)
        # UTF-8, LF, exactly the upstream bytes apart from the rewritten
        # specifiers -- so the sha384s below do not depend on the platform the
        # generator ran on.
        data = body.encode("utf-8")
        files[name] = data
        modules[name] = {"url": ORIGIN + key, "sha384": sri(data)}

    entry_files = {k: names[v] for k, v in entries.items()}
    edges = {names[k]: [names[t] for t in e.values()] for k, e in imports.items()}
    stale = _unreachable(set(entry_files.values()), modules, edges)
    if stale:
        raise SystemExit(
            f"{len(stale)} vendored module(s) no entry point can reach: "
            f"{sorted(stale)[:5]}")

    return {
        "files": files,
        "manifest": {
            "origin": ORIGIN,
            "entries": dict(sorted(entry_files.items())),
            "versions": versions,
            "modules": dict(sorted(modules.items())),
        },
    }


def dump_manifest(manifest: Dict[str, object]) -> bytes:
    """Stable bytes: sorted keys, LF, trailing newline -- so a re-run with no
    upstream change produces a byte-identical file and an empty diff."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write(out_dir: Path, files: Dict[str, bytes], manifest: Dict[str, object],
          log=print) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = set(files) | {MANIFEST}
    for stale in sorted(p for p in out_dir.iterdir() if p.name not in keep):
        stale.unlink()
        log(f"  removed stale {stale.name}")
    for name, data in sorted(files.items()):
        (out_dir / name).write_bytes(data)
    (out_dir / MANIFEST).write_bytes(dump_manifest(manifest))
    log(f"  wrote {len(files)} modules + {MANIFEST} to {out_dir}")


def check(out_dir: Path = OUT_DIR) -> List[str]:
    """Offline verification of what is committed. Returns a list of problems.

    Mirrors what ``tests/test_vendor_assets.py`` asserts, so the generator can
    be used as a quick local gate without running pytest.
    """
    problems: List[str] = []
    manifest_path = out_dir / MANIFEST
    if not manifest_path.is_file():
        return [f"{manifest_path} is missing -- run the generator"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    modules = manifest["modules"]

    on_disk = {p.name for p in out_dir.iterdir() if p.is_file()} - {MANIFEST}
    if on_disk != set(modules):
        problems.append(
            f"manifest/disk drift -- only in manifest: {sorted(set(modules) - on_disk)}; "
            f"only on disk: {sorted(on_disk - set(modules))}")

    edges: Dict[str, List[str]] = {}
    for name, meta in sorted(modules.items()):
        path = out_dir / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if sri(data) != meta["sha384"]:
            problems.append(f"{name}: sha384 does not match the manifest")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{name}: not valid UTF-8")
            continue
        if _LEFTOVER_RE.search(text):
            problems.append(f"{name}: still references esm.sh")
        edges[name] = []
        for spec in (s for _, _, s in _specifiers(text)):
            if not spec.startswith("./"):
                problems.append(f"{name}: non-relative specifier {spec!r}")
            elif spec[2:] not in modules:
                problems.append(f"{name}: imports {spec!r}, which is not vendored")
            else:
                edges[name].append(spec[2:])

    for pkg, versions in sorted(concrete_builds(
            m["url"][len(ORIGIN):] for m in modules.values()).items()):
        if len(versions) > 1:
            problems.append(f"{pkg}: {len(versions)} concrete builds ({sorted(versions)})")

    cm_ver = read_cm_ver()
    for key in cm_ver:
        if manifest["entries"].get(key) != entry_name(key):
            problems.append(f"CM_VER key {key!r} has no entry-{key}.mjs")
    for key in manifest["entries"]:
        if key not in cm_ver:
            problems.append(f"entry-{key}.mjs is vendored but CM_VER no longer has {key!r}")

    for name in sorted(_unreachable(set(manifest["entries"].values()), modules, edges)):
        problems.append(f"{name}: no entry point reaches it (stale?)")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the committed graph offline; fetch nothing")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.out)
        for problem in problems:
            print("FAIL:", problem)
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    import httpx                                    # dev-only dependency

    cm_ver = read_cm_ver()
    print(f"CM_VER: {len(cm_ver)} entry points from {EDITOR_JS.name}")
    with httpx.Client(follow_redirects=True, timeout=60,
                      headers={"user-agent": "browserland-vendor/1.0"}) as client:
        fetcher = Fetcher(client)
        entries, imports = crawl(fetcher, cm_ver)
    result = build(fetcher, entries, imports)
    write(args.out, result["files"], result["manifest"])
    for pkg, ver in result["manifest"]["versions"].items():
        print(f"  {pkg}@{ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
