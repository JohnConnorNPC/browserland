"""webterm: headless PTY agents + broker speaking Browserland's web-terminal producer protocol."""

__version__ = "0.1.0"

_BUILD_VERSION = None


def build_version() -> str:
    """A best-effort build identifier: the package version plus the webterm git
    commit short hash when running from a checkout, so a stale deployment is
    detectable (issue #22). Falls back to the bare package version when git is
    unavailable (e.g. a pip install). Computed from the package's OWN directory
    (never the process cwd), and only when that directory's parent is itself a
    git repo root — so a wheel installed *inside* an unrelated repo never reports
    that enclosing repo's commit. Cached per process; never raises.

    Example: ``"0.1.0+ba4b62e"`` from a checkout, or ``"0.1.0"`` without git.
    Uses ``rev-parse --short HEAD`` (stable across clones/tags) and does NOT
    encode a dirty-tree marker — two different dirty trees share a hash, so it
    would be a misleading equality signal. NOTE: when git is absent on BOTH the
    agent and the broker, both report the bare package version and a stale check
    by equality can't distinguish same-version-different-code; the from-checkout
    deploy (this project's norm) carries a hash and is reliable."""
    global _BUILD_VERSION
    if _BUILD_VERSION is not None:
        return _BUILD_VERSION
    ver = __version__
    try:
        import pathlib
        import subprocess
        pkg_dir = pathlib.Path(__file__).resolve().parent      # <root>/webterm
        # Only trust git when the package's parent is the repo root (a sibling
        # .git), so a pip install nested in another repo isn't mis-attributed —
        # and a non-checkout install skips the subprocess entirely.
        if (pkg_dir.parent / ".git").exists():
            out = subprocess.run(
                ["git", "-C", str(pkg_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2)
            sha = out.stdout.strip()
            if out.returncode == 0 and sha:
                ver = f"{__version__}+{sha}"
    except Exception:
        pass
    _BUILD_VERSION = ver
    return ver
