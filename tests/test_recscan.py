"""Recording secret-audit (#145).

The issue exists because output payloads are base64, so the obvious audit --
``grep <token> *.blrec`` -- finds nothing even when the token IS in the file and
reports a confident all-clear. Every test here is ultimately about not
recreating that false all-clear in a subtler form.

Fixtures are generated rather than committed: a checked-in .blrec containing a
token would be the very artifact this feature warns about.
"""

from __future__ import annotations

import base64
import json

import pytest

from webterm.broker import recscan

TOKEN = "SEKRIT-TOKEN-abcdefghijklmnop-0123456789"
MCP = "MCP-SEKRIT-zyxwvu-987654"
SECRETS = {"auth_token": TOKEN, "mcp_token": MCP}


def _b64(text) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return base64.b64encode(text).decode("ascii")


def _write(path, meta=None, events=()):
    """Compose a .blrec: line 1 meta, then one JSON event per line."""
    lines = [json.dumps(meta or {"title": "demo", "cols": 80, "rows": 24})]
    lines += [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _out(t, text):
    return {"t": t, "k": "o", "d": _b64(text)}


# ---- the core trap ------------------------------------------------------

def test_grep_would_miss_it_but_the_scanner_finds_it(tmp_path):
    """The premise of the whole issue, asserted rather than assumed."""
    p = _write(tmp_path / "rec-1.blrec", events=[_out(100, f"token is {TOKEN}\r\n")])
    # A plain grep over the file finds nothing...
    assert TOKEN not in p.read_text(encoding="utf-8")
    # ...but the scanner decodes and finds it.
    res = recscan.scan_file(p, SECRETS)
    assert [f.label for f in res.findings] == ["auth_token"]
    assert res.errors == []


def test_clean_recording_is_clean(tmp_path):
    p = _write(tmp_path / "rec-2.blrec",
               events=[_out(0, "$ ls\r\n"), _out(50, "file.txt\r\n")])
    res = recscan.scan_file(p, SECRETS)
    assert res.findings == [] and res.errors == []


# ---- split across events (the false-all-clear generator) ----------------

def test_secret_split_across_two_events(tmp_path):
    """PTY output arrives in arbitrary chunks, so a secret CAN straddle two
    events. A per-event scan would miss this and report clean."""
    half = len(TOKEN) // 2
    p = _write(tmp_path / "rec-3.blrec", events=[
        _out(10, "prefix " + TOKEN[:half]),
        _out(20, TOKEN[half:] + " suffix"),
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1
    f = res.findings[0]
    assert f.label == "auth_token"
    assert f.spans_events is True
    # Attributed to where the match STARTS, so scrubbing there shows it.
    assert f.event_index == 0 and f.t_ms == 10


def test_secret_split_across_three_events_with_a_tiny_middle(tmp_path):
    """The middle chunk is shorter than the secret, so the window must hold
    more than one previous chunk."""
    a, b = 10, 14
    p = _write(tmp_path / "rec-4.blrec", events=[
        _out(10, "x" + TOKEN[:a]),
        _out(20, TOKEN[a:b]),
        _out(30, TOKEN[b:] + "y"),
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1
    assert res.findings[0].spans_events is True
    assert res.findings[0].event_index == 0


def test_split_survives_an_intervening_resize_or_gap(tmp_path):
    """A resize/gap event between the halves must NOT reset the carry-over --
    resetting there would be a false-negative generator."""
    half = len(TOKEN) // 2
    p = _write(tmp_path / "rec-5.blrec", events=[
        _out(10, TOKEN[:half]),
        {"t": 15, "k": "r", "cols": 100, "rows": 30},
        {"t": 16, "k": "g"},
        _out(20, TOKEN[half:]),
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1, "carry-over was reset by a non-output event"


def test_each_occurrence_reported_once(tmp_path):
    """The retained window must not re-report a match it already saw."""
    p = _write(tmp_path / "rec-6.blrec", events=[
        _out(10, TOKEN), _out(20, "unrelated"), _out(30, "tail"),
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1


def test_two_distinct_occurrences_both_reported(tmp_path):
    p = _write(tmp_path / "rec-7.blrec", events=[
        _out(10, f"first {TOKEN}"), _out(900, f"again {TOKEN}"),
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 2
    assert [f.t_ms for f in res.findings] == [10, 900]


# ---- other locations ----------------------------------------------------

def test_secret_in_the_meta_title(tmp_path):
    """Many shells set the terminal title to the running command line."""
    p = _write(tmp_path / "rec-8.blrec",
               meta={"title": f"curl -H 'auth: {TOKEN}'", "cols": 80, "rows": 24},
               events=[_out(10, "ok\r\n")])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1
    assert res.findings[0].event_index == recscan.META_INDEX


def test_secret_in_an_unknown_event_string_field(tmp_path):
    """Schema drift must not create a silent blind spot: a future event kind
    carrying text is still scanned."""
    p = _write(tmp_path / "rec-9.blrec", events=[
        {"t": 10, "k": "note", "text": f"see {TOKEN}"},
    ])
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1


def test_input_markers_carry_no_content_to_find(tmp_path):
    """`i` events are a byte COUNT, never keystrokes -- nothing to match."""
    p = _write(tmp_path / "rec-10.blrec",
               events=[{"t": 10, "k": "i", "n": len(TOKEN)}])
    res = recscan.scan_file(p, SECRETS)
    assert res.findings == []


def test_mcp_token_found_independently(tmp_path):
    p = _write(tmp_path / "rec-11.blrec", events=[_out(10, f"mcp={MCP}")])
    res = recscan.scan_file(p, SECRETS)
    assert [f.label for f in res.findings] == ["mcp_token"]


# ---- damaged files: incomplete must never read as clean -----------------

def test_malformed_json_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "rec-12.blrec"
    p.write_text("\n".join([
        json.dumps({"title": "demo"}),
        "{ this is not json",
        json.dumps(_out(20, f"still scanned {TOKEN}")),
    ]) + "\n", encoding="utf-8")
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1, "a bad line must not abort the scan"


def test_undecodable_base64_is_reported_as_an_error(tmp_path):
    """An event we could not decode is an INCOMPLETE audit, and must be said
    out loud rather than counted as clean."""
    p = tmp_path / "rec-13.blrec"
    p.write_text("\n".join([
        json.dumps({"title": "demo"}),
        json.dumps({"t": 10, "k": "o", "d": "!!!not base64!!!"}),
    ]) + "\n", encoding="utf-8")
    res = recscan.scan_file(p, SECRETS)
    assert res.findings == []
    assert res.errors and "could NOT be scanned" in res.errors[0]


def test_no_secrets_supplied_is_an_error_not_a_clean_result(tmp_path):
    p = _write(tmp_path / "rec-14.blrec", events=[_out(10, TOKEN)])
    res = recscan.scan_file(p, {})
    assert res.findings == []
    assert res.errors, "scanning for nothing must not report clean"


def test_scan_dir_aggregates_and_skips_non_blrec(tmp_path):
    _write(tmp_path / "rec-a.blrec", events=[_out(10, TOKEN)])
    _write(tmp_path / "rec-b.blrec", events=[_out(10, "clean")])
    (tmp_path / "notes.txt").write_text(TOKEN, encoding="utf-8")
    res = recscan.scan_dir(tmp_path, SECRETS)
    assert len(res.findings) == 1
    assert res.findings[0].path.name == "rec-a.blrec"


def test_missing_dir_is_an_error(tmp_path):
    res = recscan.scan_dir(tmp_path / "nope", SECRETS)
    assert res.errors


# ---- documented limits: these SHOULD miss, and the tool says so ---------

def test_ansi_interleaved_secret_is_a_known_miss(tmp_path):
    """A secret broken up by colour escapes is NOT contiguous in the byte
    stream, so a literal scan cannot see it.

    Pinned deliberately: this is the tool's honest limit, printed in its own
    output. If someone later makes the scanner ANSI-aware, this test failing is
    the signal to update that disclaimer -- not to delete the test."""
    mid = len(TOKEN) // 2
    interleaved = TOKEN[:mid] + "\x1b[31m" + TOKEN[mid:]
    p = _write(tmp_path / "rec-15.blrec", events=[_out(10, interleaved)])
    res = recscan.scan_file(p, SECRETS)
    assert res.findings == [], "if this now passes, update the CLI's NOTE"


def test_rotated_secret_is_only_found_via_the_secret_flag(tmp_path):
    """The scanner searches for values it is GIVEN. A rotated token is invisible
    unless the operator passes it explicitly -- which is what --secret is for."""
    old = "OLD-ROTATED-TOKEN-11112222"
    p = _write(tmp_path / "rec-16.blrec", events=[_out(10, f"old={old}")])
    assert recscan.scan_file(p, SECRETS).findings == []
    res = recscan.scan_file(p, dict(SECRETS, **{"--secret[0]": old}))
    assert len(res.findings) == 1


# ---- line wrap: the thing people assume breaks it, and doesn't ----------

def test_an_80_column_wrap_does_not_break_the_scan(tmp_path):
    """Wrapping is what the TERMINAL does when rendering; the PTY byte stream
    has no newline inserted, so the secret is still contiguous."""
    long_line = "x" * 75 + TOKEN + "\r\n"     # visually wraps at 80 cols
    p = _write(tmp_path / "rec-17.blrec", events=[_out(10, long_line)])
    assert len(recscan.scan_file(p, SECRETS).findings) == 1


# ---- the CLI contract ---------------------------------------------------

def _cli(tmp_path, *args, env=None):
    """Run --scan-recordings in a fresh interpreter; returns (rc, out, err)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    e = dict(os.environ)
    e["PYTHONPATH"] = str(repo) + os.pathsep + e.get("PYTHONPATH", "")
    e.pop("WEB_TERMINAL_TOKEN", None)
    e.pop("WEB_TERMINAL_CONFIG", None)
    e.pop("WEB_TERMINAL_MCP_TOKEN", None)
    e.update(env or {})
    p = subprocess.run(
        [sys.executable, "-m", "webterm.broker", "--scan-recordings", *args],
        cwd=str(tmp_path), env=e, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def test_cli_never_mints_a_token_while_auditing(tmp_path):
    """Hard requirement: asking "is there a secret in my recordings" must not
    CREATE a secret. A mint here would also search for the wrong value -- one
    the broker isn't even running."""
    (tmp_path / "webterm_recordings").mkdir()
    rc, out, err = _cli(tmp_path)
    assert not (tmp_path / "webterm_token.json").exists(), \
        "the audit minted a token sidecar"
    # ...and with nothing to search for it must say so, not report clean.
    assert rc == 2, (rc, out, err)
    assert "nothing to search for" in err


def test_cli_exit_codes_and_disclaimer(tmp_path):
    recs = tmp_path / "webterm_recordings"
    recs.mkdir()
    _write(recs / "rec-clean.blrec", events=[_out(10, "nothing here")])
    rc, out, _ = _cli(tmp_path, env={"WEB_TERMINAL_TOKEN": TOKEN})
    assert rc == 0, out
    assert "no configured secret found" in out
    # The honest limit is printed even on a CLEAN result -- otherwise this tool
    # recreates the false all-clear it exists to prevent.
    assert "evidence, not proof" in out

    _write(recs / "rec-dirty.blrec", events=[_out(10, f"tok {TOKEN}")])
    rc, out, _ = _cli(tmp_path, env={"WEB_TERMINAL_TOKEN": TOKEN})
    assert rc == 1, out
    assert "FOUND auth_token" in out and "rec-dirty.blrec" in out
    assert "evidence, not proof" in out


def test_cli_json_mode_is_machine_readable(tmp_path):
    recs = tmp_path / "webterm_recordings"
    recs.mkdir()
    _write(recs / "rec-j.blrec", events=[_out(125_000, f"tok {TOKEN}")])
    rc, out, _ = _cli(tmp_path, "--json", env={"WEB_TERMINAL_TOKEN": TOKEN})
    assert rc == 1
    payload = json.loads(out)
    assert payload["scannedFor"] == ["auth_token"]
    assert len(payload["findings"]) == 1
    f = payload["findings"][0]
    assert f["secret"] == "auth_token" and f["timestampMs"] == 125_000
    # 125_000 ms = 2 min 5 s -- pins the mm:ss rollover, not just seconds.
    assert f["clock"] == "2:05"


def test_cli_secret_flag_finds_a_rotated_token(tmp_path):
    recs = tmp_path / "webterm_recordings"
    recs.mkdir()
    old = "OLD-ROTATED-9988776655"
    _write(recs / "rec-r.blrec", events=[_out(10, f"old={old}")])
    rc, _, _ = _cli(tmp_path, env={"WEB_TERMINAL_TOKEN": TOKEN})
    assert rc == 0, "the current token is genuinely absent"
    rc, out, _ = _cli(tmp_path, "--secret", old,
                      env={"WEB_TERMINAL_TOKEN": TOKEN})
    assert rc == 1 and "FOUND" in out


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16])
def test_found_however_finely_the_output_is_chunked(tmp_path, chunk):
    """The strongest form of the carry-over invariant: byte-at-a-time output
    must still be found."""
    blob = f"prefix {TOKEN} suffix"
    events = [_out(i * 10, blob[i:i + chunk])
              for i in range(0, len(blob), chunk)]
    p = _write(tmp_path / f"rec-chunk-{chunk}.blrec", events=events)
    res = recscan.scan_file(p, SECRETS)
    assert len(res.findings) == 1, f"missed with {chunk}-byte chunks"
