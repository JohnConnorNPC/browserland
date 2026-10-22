"""#260 review: the docs say what the code does, and their numbers come from
the code's own constants (the #261 MAX_MCP_PEERS pattern), so changing the
delay, the cap or the token floor without the docs reds here."""

from __future__ import annotations

from pathlib import Path

from webterm.broker import auth
from webterm.broker.app import ADMIN_TOKEN_MIN_CHARS

ROOT = Path(__file__).resolve().parents[1]

#: The delay in words, as the docs spell it. A new value has no entry, so a
#: changed AUTH_FAIL_DELAY_S fails here until the docs (and this map) follow.
_SECONDS = {1.0: "one second"}


def _flat(text):
    return " ".join(text.split())


def _page(rel):
    return _flat((ROOT / rel).read_text(encoding="utf-8"))


def _section(rel, head):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert text.count(head) == 1, head
    a = text.index(head)
    return _flat(text[a:text.index("\n---", a)])


def _changelog_item(lead):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    a = text.index(lead)
    return _flat(text[a:text.index("\n\n", a)])


def _delay_words():
    # The source's value: the suite's conftest zeroes the live constant.
    from tests.conftest import REAL_AUTH_FAIL_DELAY_S
    return _SECONDS[REAL_AUTH_FAIL_DELAY_S]


def test_tr_states_the_delay_and_its_cap():
    tr = _page("wiki/Technical-Reference.md")
    assert (f"A wrong token's 401 is answered after {_delay_words()} while "
            f"fewer than {auth.AUTH_FAIL_CONCURRENT} are waiting, and at once "
            "beyond that") in tr
    assert ("on every browser gate, `/mcp/*`, `/peer/*`, `/ws` and "
            "`/control`") in tr


def test_tr_names_what_is_not_delayed():
    tr = _page("wiki/Technical-Reference.md")
    assert ("Not delayed: the producer `/browserland` dial and the help "
            "corpus") in tr


def test_tr_states_the_browser_token_floor_only():
    tr = _page("wiki/Technical-Reference.md")
    assert (f"A resolved browser token shorter than {ADMIN_TOKEN_MIN_CHARS} "
            "characters refuses to start; the MCP token has no such floor "
            "yet") in tr


def test_tr_says_where_the_stripped_mcp_variables_come_from():
    tr = _page("wiki/Technical-Reference.md")
    assert ("When the environment pins them, they come from the environment "
            "you set for the broker") in tr
    assert "`--print-token` prints only the browser token." in tr
    assert "read them with `--print-token` or from the sidecars" not in tr


def test_upgrading_scopes_the_floor_to_the_browser_token():
    up = _section("wiki/Upgrading.md",
                  "## A chosen token needs 16 or more characters (#260)")
    assert (f"A broker whose browser token is shorter than "
            f"{ADMIN_TOKEN_MIN_CHARS} characters refuses to start") in up
    assert "The MCP token has no such floor yet" in up
    assert "every place a token can be set" not in up
    assert (f"arrives {_delay_words()} late (at most "
            f"{auth.AUTH_FAIL_CONCURRENT} at once)") in up


def test_upgrading_says_terminals_no_longer_inherit_the_mcp_tokens():
    up = _section("wiki/Upgrading.md",
                  "## Terminals no longer inherit the MCP tokens (#260)")
    assert "no longer inherits" in up
    assert "It now starts without it." in up
    assert "**Copy .mcp.json**" in up


def test_changelog_states_what_is_and_is_not_delayed():
    item = _changelog_item("- **A wrong token's 401 is answered after a delay**")
    assert (f"a wrong token waits {_delay_words()} before its 401, while at "
            f"most {auth.AUTH_FAIL_CONCURRENT} wait at once") in item
    assert ("The producer `/browserland` dial and the help corpus are not "
            "delayed.") in item
    assert "slow" not in item


def test_changelog_scopes_the_floor_to_the_browser_token():
    item = _changelog_item("- **A chosen token needs 16 or more characters**")
    assert "browser `auth_token` is shorter" in item
    assert "the MCP token has no floor yet" in item


def test_tr_states_the_admin_token_bad_token_rule():
    # H3: the POST /mcp/config clauses.
    tr = _page("wiki/Technical-Reference.md")
    assert "clears it; any other string is refused" in tr
    assert ("`bad_token` (400, non-string `token`/`admin_token`, or a "
            "non-empty `admin_token` string)") in tr


def test_installing_mods_says_the_pane_warns_without_an_admin_token():
    # H1: the Mods-pane note, where an operator installs.
    im = _page("wiki/Installing-Mods.md")
    assert ("If no admin token is set, the Mods pane says so at the top: then "
            "any page or mod holding the broker's password can install and "
            "uninstall mods there.") in im
