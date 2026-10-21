import sys
from pathlib import Path

# Run tests against the in-tree package without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from webterm.broker import auth as _auth  # noqa: E402

#: The delay as the source defines it, read before any test zeroes it; the
#: tests that pin the delay put it back from here (#260).
REAL_AUTH_FAIL_DELAY_S = _auth.AUTH_FAIL_DELAY_S


@pytest.fixture(autouse=True)
def _no_wrong_token_delay(monkeypatch):
    """#260: a wrong token's 401 waits a second (auth.AUTH_FAIL_DELAY_S), and
    much of the suite answers wrong tokens on purpose. create_app reads the
    constant at call time, so zero it for every test; the tests that pin the
    delay set the real value back before they build their app."""
    monkeypatch.setattr(_auth, "AUTH_FAIL_DELAY_S", 0)
