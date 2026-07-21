"""Token-appending wrappers around the Sanic test clients (#142).

A token is now required on every surface — there is no loopback exemption — so
the in-process test clients have to carry one. ``authed(app)`` and
``authed_reusable(app)`` are drop-in replacements for ``app.test_client`` and
``ReusableClient(app)`` that append ``?token=`` read from **app.ctx.auth_token**,
so they work whether the app under test configured a token or minted one.

Deliberately NOT a conftest shim over ``sanic_testing``'s private
``_sanic_endpoint_test``: its signature differs between ``testing.py`` and
``reusable.py``, and a blanket shim would silently defeat the auth-NEGATIVE
assertions (``test_file_api.py``'s route guard, the /info, /status/fetch and
/profiles/config 401 tests). Those keep using the raw client on purpose — an
opt-in wrapper cannot accidentally authenticate a test that meant not to be.
"""

from __future__ import annotations

import urllib.parse
from contextlib import contextmanager

from sanic_testing.reusable import ReusableClient

#: Default token for app factories that don't care about the value. Every test
#: app must configure one EXPLICITLY: a factory that left auth_token unset would
#: make the broker mint a sidecar, and the suite runs with cwd=<repo>, so that
#: would drop a real secret beside the source tree.
TEST_TOKEN = "test-token"

#: Client methods whose first positional argument is a URL.
_URL_METHODS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "request",
    "websocket",
})


def with_token(url: str, token: str) -> str:
    """``url`` with ``?token=`` appended. An explicit token/auth already in the
    query is left alone, so a test can still pass a deliberately WRONG one."""
    if not token:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(key in ("token", "auth") for key, _ in query):
        return url
    query.append(("token", token))
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query)))


class _AuthedClient:
    """Thin proxy: URL-taking methods get the token, everything else passes
    straight through to the wrapped client."""

    def __init__(self, client, token: str) -> None:
        self._client = client
        self._token = token

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in _URL_METHODS or not callable(attr):
            return attr

        def call(url, *args, **kwargs):
            return attr(with_token(url, self._token), *args, **kwargs)

        return call


def authed(app):
    """``app.test_client`` with this app's token on every request."""
    return _AuthedClient(app.test_client, app.ctx.auth_token)


@contextmanager
def authed_reusable(app):
    """``ReusableClient(app)`` with this app's token on every request — one
    server across many requests, for the stateful upload/recording flows whose
    in-memory session state a single-request client would drain."""
    with ReusableClient(app) as client:
        yield _AuthedClient(client, app.ctx.auth_token)
