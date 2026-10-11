"""Direct Gmail boundary; every mutation requires explicit capability enablement."""

import base64
import json
import logging
import os
from pathlib import Path

from .errors import CleanupError, MessageNotFound
from .storage import write_private_json

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def decode_raw(value):
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise CleanupError("Invalid raw MIME in Gmail response") from None


def authenticate(credentials_path, token_path, modify=False, drive=False):
    """User-invoked desktop OAuth only; never looks at connectors or environment tokens."""
    try:
        target = Path(token_path).expanduser()
        if target.exists() or target.is_symlink():
            raise CleanupError("Token file exists; use a new path instead of overwriting credentials")
        from google_auth_oauthlib.flow import InstalledAppFlow
        scopes = [MODIFY_SCOPE if modify else READ_SCOPE]
        if drive:
            scopes.append(DRIVE_SCOPE)
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
        # The library's loopback request logger otherwise can include OAuth codes
        # when the caller enables INFO logging. Do not print the consent URL either.
        logger = logging.getLogger("google_auth_oauthlib.flow")
        was_disabled = logger.disabled
        logger.disabled = True
        try:
            credentials = flow.run_local_server(
                host="localhost", port=0, open_browser=True, timeout_seconds=180,
                prompt="consent",
                authorization_prompt_message="Complete authorization in your local browser.",
                success_message="Authorization complete. You may close this window.")
        finally:
            logger.disabled = was_disabled
        if not credentials.refresh_token:
            raise CleanupError("Google did not return a refresh token; repeat desktop OAuth with offline consent")
        write_private_json(token_path, json.loads(credentials.to_json()), exclusive=True)
    except CleanupError:
        raise
    except FileExistsError:
        raise CleanupError("Token file exists; use a new path instead of overwriting credentials") from None
    except ImportError:
        raise CleanupError("Install the optional gmail dependencies first") from None
    except Exception:
        raise CleanupError("OAuth setup failed; verify desktop credentials and local browser access") from None


class Gmail:
    @classmethod
    def from_environment(cls, require_modify=False):
        """Refresh in memory; proxy secrets go only to the fixed HTTPS token endpoint.

        Never materialize an environment secret/placeholder in a local token file.
        This factory does not read messages or make any Gmail API request.
        """
        required = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_OAUTH_SCOPE")
        if any(not os.environ.get(name) for name in required):
            raise CleanupError("Missing Gmail environment bindings; configure client ID, client secret, refresh token and OAuth scope")
        scopes = os.environ["GMAIL_OAUTH_SCOPE"].split()
        gmail_scopes = set(scopes) & {READ_SCOPE, MODIFY_SCOPE}
        if len(gmail_scopes) != 1 or set(scopes) - {READ_SCOPE, MODIFY_SCOPE, DRIVE_SCOPE, DRIVE_READ_SCOPE}:
            raise CleanupError("GMAIL_OAUTH_SCOPE requires one Gmail scope and optional drive.file/drive.readonly scope URLs")
        if require_modify and MODIFY_SCOPE not in scopes:
            raise CleanupError("Environment OAuth grant is read-only; explicit gmail.modify authorization is required")
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            credentials = Credentials(
                token=None, client_id=os.environ["GMAIL_CLIENT_ID"],
                client_secret=os.environ["GMAIL_CLIENT_SECRET"],
                refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
                token_uri="https://oauth2.googleapis.com/token", scopes=scopes)
            credentials.refresh(Request())
            granted = credentials.granted_scopes
            if granted is not None and not set(scopes).issubset(granted):
                raise CleanupError("Google did not grant the configured Gmail OAuth scope")
            if not credentials.valid:
                raise CleanupError("Environment OAuth credentials are not usable")
            instance = cls.__new__(cls)
            instance._configure(credentials, require_modify)
            return instance
        except CleanupError:
            raise
        except ImportError:
            raise CleanupError("Install the optional gmail dependencies first") from None
        except Exception:
            raise CleanupError("Environment OAuth refresh failed; check secure bindings, Google test-user access, token expiry and network policy") from None

    def _configure(self, credentials, require_modify):
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
        ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=30, ca_certs=ca_bundle))
        self.credentials = credentials
        self.http = http
        self.service = build("gmail", "v1", http=http,
                             cache_discovery=False, static_discovery=True)
        self.can_modify = require_modify

    @property
    def drive(self):
        from .drive import Drive
        return Drive(self.credentials, self.http)

    def __init__(self, token_path, require_modify=False):
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            path = Path(token_path).expanduser()
            if path.stat().st_mode & 0o077:
                raise CleanupError("OAuth token file must be private (chmod 600)")
            # Check recorded grant, not just requested scopes passed to the client.
            info = json.loads(path.read_text())
            granted = set(info.get("scopes", []))
            acceptable = {MODIFY_SCOPE} if require_modify else {READ_SCOPE, MODIFY_SCOPE}
            if not granted & acceptable:
                raise CleanupError("Token lacks the required explicit OAuth grant; authenticate again")
            credentials = Credentials.from_authorized_user_info(info)
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                write_private_json(path, json.loads(credentials.to_json()))
            if not credentials.valid:
                raise CleanupError("OAuth credentials are not usable; authenticate again")
            actual_grant = set(credentials.granted_scopes if credentials.granted_scopes is not None else granted)
            if not actual_grant & acceptable:
                raise CleanupError("Refreshed OAuth grant lacks the required Gmail capability")
            self._configure(credentials, require_modify)
        except CleanupError:
            raise
        except ImportError:
            raise CleanupError("Install the optional gmail dependencies first") from None
        except Exception:
            raise CleanupError("Could not initialize Gmail authentication") from None

    @staticmethod
    def _execute(request):
        try:
            # Do not automatically retry a non-idempotent mailbox insert.
            result = request.execute(num_retries=0)
            if not isinstance(result, dict):
                raise CleanupError("Invalid Gmail API response")
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                raise MessageNotFound("Gmail message or thread was not found (HTTP 404)") from None
            suffix = f" (HTTP {status})" if isinstance(status, int) else ""
            raise CleanupError("Gmail API request failed" + suffix) from None

    def get(self, message_id):
        data = self._execute(self.service.users().messages().get(
            userId="me", id=message_id, format="raw",
            fields="id,threadId,labelIds,internalDate,raw,classificationLabelValues"))
        if not isinstance(data.get("raw"), str):
            raise CleanupError("Gmail response is missing raw MIME")
        data["raw_bytes"] = decode_raw(data.pop("raw"))
        return data

    def insert(self, raw, labels, thread_id=None):
        if not self.can_modify:
            raise CleanupError("Write-capable authentication was not explicitly enabled")
        body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii"), "labelIds": labels}
        if thread_id:
            body["threadId"] = thread_id
        # Deliberately omit internalDate (not documented as an insert override).
        return self._execute(self.service.users().messages().insert(
            userId="me", internalDateSource="dateHeader", body=body))

    def get_thread(self, thread_id):
        return self._execute(self.service.users().threads().get(
            userId="me", id=thread_id, format="minimal", fields="id,messages/id"))

    def trash(self, message_id):
        if not self.can_modify:
            raise CleanupError("Write-capable authentication was not explicitly enabled")
        return self._execute(self.service.users().messages().trash(userId="me", id=message_id))



class OfflineMailbox:
    """Read-only source inventory for fixture development, never a write fallback."""
    def __init__(self, path):
        try:
            self.path = Path(path).resolve()
            self.sources = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise CleanupError("Cannot read offline source inventory") from None

    def get(self, message_id):
        try:
            item = dict(self.sources[message_id])
            location = self.path.parent / item.pop("raw_file")
            item.update(id=message_id, raw_bytes=location.read_bytes())
            return item
        except (KeyError, OSError, TypeError):
            raise CleanupError("Message is absent from offline source inventory") from None

    def insert(self, *args, **kwargs):
        raise CleanupError("Offline source inventories cannot insert mail")

    def trash(self, *args, **kwargs):
        raise CleanupError("Offline source inventories cannot trash mail")
