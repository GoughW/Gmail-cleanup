"""Single-submission adapter for the exact httplib2/Google SDK lock.

No connection/redirect/challenge/401 response replay, even for allocation GETs.
Connection construction, proxy routing, TLS and CA verification stay upstream.
"""
import importlib.metadata
import httplib2
from google_auth_httplib2 import AuthorizedHttp

from .errors import CleanupError

STACK = {"httplib2": "0.32.0", "google-auth-httplib2": "0.4.4",
         "google-api-python-client": "2.201.0", "google-auth": "2.61.0"}


def require_locked_transport():
    if any(importlib.metadata.version(name) != version for name, version in STACK.items()):
        raise CleanupError("Unreviewed SDK/transport version; install requirements.lock")


class SingleAttemptHttp(httplib2.Http):
    def _conn_request(self, conn, request_uri, method, body, headers):
        try:
            if conn.sock is None:
                conn.connect()
            conn.request(method, request_uri, body, headers)
            response = conn.getresponse()
            content = response.read() if method != "HEAD" else b""
            result = httplib2.Response(response)
            if method == "HEAD":
                conn.close()
            else:
                content = httplib2._decompressContent(result, content, self.limit_kwargs)
            return result, content
        finally:
            conn.close()

    def _request(self, conn, host, absolute_uri, request_uri, method, body, headers,
                 redirections, cachekey):
        # Bypass httplib2 challenge and redirect machinery, not connection security.
        return self._conn_request(conn, request_uri, method, body, headers)


def authorized_transport(credentials, **options):
    require_locked_transport()
    # before_request refreshes expired credentials before the first submission.
    # A 401 after submission returns to recovery instead of refreshing/replaying.
    return AuthorizedHttp(credentials, http=SingleAttemptHttp(**options), max_refresh_attempts=0)
