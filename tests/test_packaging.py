"""The package version has exactly ONE source: ``webterm.__version__``.

``pyproject.toml`` used to carry a static ``version = "0.1.0"`` alongside
``webterm/__init__.py``'s ``__version__``, with nothing keeping the pair in
sync -- so a bump could (and did) land in one and not the other, and a wheel
could advertise a version the code it contains disagrees with. The fix is
setuptools' dynamic-attr route: pyproject declares ``dynamic = ["version"]``
and points ``[tool.setuptools.dynamic]`` at the attribute.

These guards pin the whole chain, offline and without a build:

* the backend really is setuptools, at a version that understands the attr
  (the ``{attr = ...}`` spelling is setuptools>=61 -- under another backend it
  is inert decoration);
* no static ``version =`` has crept back into ``[project]`` next to the
  dynamic declaration;
* the attr the build reads is ``webterm.__version__`` and nothing else;
* that attribute is bound exactly ONCE, to a plain string literal, resolvable
  the way setuptools resolves it (``ast``, no import), and equal to the value
  the imported package exposes;
* the literal is already PEP 440-canonical and carries no local segment, since
  ``build_version()`` appends its own ``+<sha>``.

The single-binding rule matters beyond tidiness: ``read_attr`` only stays
import-free while the assignment is a literal. Rebind ``__version__`` to
anything computed -- even to the same string -- and a wheel build starts
IMPORTING webterm to get it, a silent change in what a build executes.

pyproject is read as text rather than parsed: tomllib is 3.11+ and this package
supports 3.9, so a parser would mean a new test dependency. To stay honest
about table scoping anyway, ``_keys_in`` walks the file tracking the current
``[table]`` header and only reports keys from the table asked for -- a decoy
``version = "9.9.9"`` under some unrelated tool table is correctly ignored.
"""

import ast
import re
from pathlib import Path

import webterm

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
VERSION_ATTR = "webterm.__version__"

_TABLE = re.compile(r"^\[([^\]]+)\]\s*$")
_KEY = re.compile(r'^\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_.-]+))\s*=\s*(.*?)\s*$')


def _keys_in(table: str):
    """``{key: raw_value}`` for one top-level ``[table]`` of pyproject.toml.

    Deliberately small: enough to say WHICH table a key sits in (the thing a
    flat regex over the file cannot), not a TOML parser. Multi-line values are
    reported as their first line, which is all these assertions read."""
    out, current = {}, None
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        head = _TABLE.match(line)
        if head:
            current = head.group(1).strip()
            continue
        if current != table:
            continue
        m = _KEY.match(line)
        if m:
            out[m.group(1) or m.group(2) or m.group(3)] = m.group(4)
    return out


def test_the_package_under_test_is_this_checkout():
    # Every other assertion here compares pyproject.toml in THIS tree against
    # the imported webterm. If pytest is importing some other installed copy
    # (this dev box has an unrelated editable install of an older checkout),
    # the comparison is meaningless rather than wrong -- so say so loudly.
    assert Path(webterm.__file__).resolve().parent.parent == ROOT, (
        f"imported webterm is {webterm.__file__}, not the one under {ROOT}; "
        "these packaging guards would be comparing two different trees")


def test_build_backend_is_setuptools_new_enough_for_the_attr():
    # `[tool.setuptools.dynamic] version = {attr = ...}` is setuptools syntax,
    # and pyproject-driven dynamic metadata landed in setuptools 61. Swap the
    # backend, or drop the floor, and the version silently stops resolving from
    # the code -- under an isolated build, from whatever setuptools pip picks.
    build = _keys_in("build-system")
    assert build.get("build-backend") == '"setuptools.build_meta"'
    req = build.get("requires", "")
    m = re.search(r'"setuptools\s*>=\s*(\d+)', req)
    assert m and int(m.group(1)) >= 61, \
        f"requires must floor setuptools at >=61 for the dynamic attr; got {req}"


def test_version_is_declared_dynamic_and_not_also_static():
    project = _keys_in("project")
    dynamic = project.get("dynamic", "")
    assert "version" in re.findall(r'"([^"]+)"', dynamic), \
        f'[project] must declare dynamic = ["version"]; got {dynamic!r}'
    # A static version in [project] is the second source of truth this file
    # exists to prevent -- and setuptools rejects declaring both anyway, so
    # this fails at build time too; better to fail here, instantly.
    assert "version" not in project, \
        "[project] carries a static version again, next to the dynamic one"


def test_dynamic_version_points_at_the_package_attribute():
    dyn = _keys_in("tool.setuptools.dynamic")
    assert "version" in dyn, \
        "[tool.setuptools.dynamic] must resolve version from an attr"
    m = re.match(r'\{\s*attr\s*=\s*["\']([^"\']+)["\']\s*\}', dyn["version"])
    assert m, f"expected {{attr = ...}}, got {dyn['version']!r}"
    assert m.group(1) == VERSION_ATTR


def test_version_attr_resolves_statically_to_the_imported_version():
    """Resolve the attr the way setuptools' ``read_attr`` does -- parse the
    module and read the literal, no import -- and require it to equal what the
    imported package reports. A mismatch means a built wheel would carry a
    different version than the running code says it is.

    Every binding in the module is collected, not just the first: a literal
    followed by a computed rebinding would satisfy a first-match check while
    forcing setuptools to import webterm at build time to see the real value."""
    module, _, attr = VERSION_ATTR.rpartition(".")
    src = ROOT.joinpath(*module.split(".")) / "__init__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    bound = []          # every value ever bound to __version__, in source order
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == attr for t in targets):
            bound.append(node.value)

    assert len(bound) == 1, (
        f"{attr} must be bound exactly once so setuptools can read it without "
        f"importing webterm; found {len(bound)} bindings")
    assert isinstance(bound[0], ast.Constant) and isinstance(bound[0].value, str), \
        f"{attr} must be a plain string literal, not a computed value"
    assert bound[0].value == webterm.__version__


def test_version_literal_is_canonical_and_carries_no_local_segment():
    # setuptools NORMALIZES the version into wheel metadata (PEP 440), so a
    # non-canonical spelling here would reintroduce exactly the disagreement
    # this file exists to prevent: `0.9.0-rc1` in the code, `0.9.0rc1` in the
    # dist. And build_version() appends its own `+<sha>`, so a local segment
    # already in __version__ would compose into an invalid `a+b+sha`.
    v = webterm.__version__
    assert "+" not in v, \
        "__version__ must not carry a local segment; build_version() adds +<sha>"
    assert re.fullmatch(r"\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?", v), \
        f"__version__ {v!r} is not a canonical PEP 440 release string"


def test_build_version_extends_the_single_source():
    # The build id is the package version, optionally plus the git short hash
    # -- never a second version scheme, and never anything but a hash after it.
    v = webterm.build_version()
    assert v.startswith(webterm.__version__)
    local = v[len(webterm.__version__):]
    assert local == "" or re.fullmatch(r"\+[0-9a-f]{7,40}", local), \
        f"unexpected build_version() suffix: {local!r}"
