"""Foreground-agent classifier, including the vendor-dir path fallback
(todo2 task 11: grok ships as a generically-named ``agent.exe`` under
``…\\.grok\\bin\\``, so name/argv rules miss it)."""

from webterm.agent.detect import classify_proc


def test_basename_match():
    assert classify_proc("claude.exe", ["claude.exe"]) == "claude"
    assert classify_proc("codex", []) == "codex"


def test_argv0_match():
    assert classify_proc("node.exe", ["C:\\x\\opencode", "serve"]) == "opencode"


def test_interpreter_first_script_only():
    assert classify_proc("node", ["node", "claude", "--foo"]) == "claude"
    # Only the FIRST script-like arg is inspected.
    assert classify_proc("node", ["node", "server.js", "codex"]) is None


def test_no_false_positive_on_arguments():
    # `rg codex` / `cat claude.md` must NOT trip.
    assert classify_proc("rg", ["rg", "codex"]) is None
    assert classify_proc("cat", ["cat", "claude.md"]) is None


def test_grok_vendor_dir_in_exe_path():
    """The reported case: grok installed as agent.exe under .grok\\bin.
    Basename 'agent' matches nothing, but the .grok dir in the exe path does."""
    exe = "C:\\Users\\Administrator\\.grok\\bin\\agent.exe"
    assert classify_proc("agent.exe", ["agent.exe"], exe) == "grok"


def test_vendor_dir_posix_path():
    assert classify_proc("agent", ["agent"],
                         "/home/u/.opencode/bin/agent") == "opencode"
    assert classify_proc("node", ["node", "index.js"],
                         "/home/u/.codex/cli/node") == "codex"


def test_vendor_dir_from_argv0_when_no_exe():
    # Falls back to argv[0]'s path when exe is unavailable.
    assert classify_proc("agent.exe",
                         ["C:\\x\\.claude\\bin\\agent.exe"]) == "claude"


def test_vendor_dir_does_not_override_real_name():
    # A real basename match wins over the path heuristic.
    exe = "C:\\Users\\Administrator\\.grok\\bin\\claude.exe"
    assert classify_proc("claude.exe", ["claude.exe"], exe) == "claude"


def test_unrelated_path_is_none():
    assert classify_proc("agent.exe", ["agent.exe"],
                         "C:\\Program Files\\Foo\\agent.exe") is None
