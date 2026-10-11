# User-owned Google Cloud and OAuth setup

This guide configures direct Gmail/Drive API access. Credentials do not authorize
mailbox changes. The completed live test used a disposable account; a real-mailbox
pilot requires its own account check, private manifest, state database and approval.

## Project, APIs and exact scopes

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a
   project you own. Under **APIs & Services → Library**, enable **Gmail API** and
   **Google Drive API**. A service account is not this user-consent flow.
2. In **Google Auth Platform**, configure Branding, Audience and developer contact.
   Use External for personal Gmail, or Internal for an eligible Workspace project.
   During development use Testing and add the account being authorized to Test users.
3. Under **Data Access**, add exactly these production scopes:

   ```text
   https://www.googleapis.com/auth/gmail.modify
   https://www.googleapis.com/auth/drive.file
   ```

   Gmail modify covers fetching, inserting, verifying and moving messages to Trash.
   Drive file covers app-created or explicitly app-authorized backup files/folders.
   It does not grant general access to all archived Drive documents. No additional
   scope is required for this workflow. Scope grants remain broad capabilities;
   execution still requires exact manifests and separate approval.
4. Under **Clients**, create a **Desktop app** OAuth client and download its JSON
   privately. Do not use a connector's credentials. You do not need a deployed site
   or a manually configured web redirect; desktop consent uses a loopback callback.

Gmail modify is a restricted scope. Personal use and distributing an application
have different Google verification requirements. External/Testing refresh tokens
with these scopes normally expire after seven days; publishing the Codex environment
does not change OAuth app status or token lifetime. Workspace policy can restrict consent.

## Obtain credentials on your own computer

Use Python 3.11+ on the computer running your browser. Store all credential files in
a private directory outside git (or `.private/`), with file permissions 600. With
the project installed using `pip install -e '.[gmail]'`:

```sh
gmail-cleanup auth --credentials .private/gmail-oauth-desktop.json \
  --token .private/gmail-drive-oauth-token.json --modify --drive
```

Alternatively, this standalone script needs only google-auth-oauthlib:

```sh
python3 -m venv .private/oauth-venv
.private/oauth-venv/bin/python -m pip install 'google-auth-oauthlib>=1.2,<2'
.private/oauth-venv/bin/python - <<'PY'
import json
import logging
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

target = Path('.private/gmail-drive-oauth-token.json')
scopes = ['https://www.googleapis.com/auth/gmail.modify',
          'https://www.googleapis.com/auth/drive.file']
flow = InstalledAppFlow.from_client_secrets_file('.private/gmail-oauth-desktop.json', scopes)
logging.getLogger('google_auth_oauthlib.flow').disabled = True
credentials = flow.run_local_server(
    host='localhost', port=0, open_browser=True, timeout_seconds=180, prompt='consent',
    authorization_prompt_message='Complete authorization in your local browser.',
    success_message='Authorization complete. You may close this window.')
if not credentials.refresh_token:
    raise SystemExit('No refresh token returned; repeat Desktop consent.')
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(json.loads(credentials.to_json()), output, indent=2)
print('Private OAuth token saved; no Gmail or Drive file request was made.')
PY
```

Choose the intended account deliberately and verify the app/project and both scopes
in the consent screen. The token belongs to that account, not necessarily the
project owner. Use a fresh token filename when repeating consent; commands refuse
overwriting credential files. On Windows, adapt the virtualenv Python path; the
tool's private-file permission checks were tested on Linux.

## Secure environment bindings

Open environment settings. Read credential fields only in a private local editor;
never paste them into chat, logs, scripts or git. Do not materialize cloud proxy
placeholders as token files or extract credentials from a connector.

| Binding | Source and destination |
| --- | --- |
| `GMAIL_CLIENT_ID` | Desktop JSON's installed.client_id; ordinary variable. |
| `GMAIL_CLIENT_SECRET` | Same client's installed.client_secret; secure secret routed only to oauth2.googleapis.com. |
| `GMAIL_REFRESH_TOKEN` | Newly consented token's refresh_token; secure secret routed only to oauth2.googleapis.com. |
| `GMAIL_OAUTH_SCOPE` | Ordinary variable containing the exact space-separated scope value below. |

```text
https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive.file
```

Changing that scope variable does not extend Google's granted scopes: repeat local
consent and update the existing refresh-token binding. Keep client ID/secret paired
with the client that issued the token. Desktop OAuth JSON is not an application
default credential; do not assign it to GOOGLE_APPLICATION_CREDENTIALS.

## Exact runtime hosts and environment publication

| Required cloud runtime host | Purpose |
| --- | --- |
| `oauth2.googleapis.com` | OAuth refresh; only destination for the two injected secrets. |
| `gmail.googleapis.com` | Gmail message/thread reads, insert and Trash. |
| `www.googleapis.com` | Drive metadata, ID allocation, upload and full binary download. |

No cloud consent browser is needed. Your local consent browser additionally needs
`accounts.google.com` and local token exchange needs `oauth2.googleapis.com`.
Google Cloud Console and `developers.google.com` documentation links are setup/
reference browsing, not runtime API requirements. Python installation needs
`pypi.org` and `files.pythonhosted.org`; preserve existing package-manager/Git
presets in a development environment. No wildcard is required for the API hosts.
Keep the inherited proxy and trusted CA bundle; never disable TLS verification.

Retained-copy links use Drive's canonical `webViewLink`. Opening them requires
browser access to `drive.google.com` and a session signed into an owning/authorized
account. That browser host is separate from the three cloud API runtime hosts.
The tool does not change file sharing permissions or fetch authenticated browser
pages; an OAuth API-access check is not a browser-session check.

Review/save the environment settings, then use the product's **Publish** action.
Draft saving does not publish the environment. The available agent tools save
installation/startup instructions and host configuration; publication is a product
UI action. A snapshot can contain ignored `.private/` files as well as source code:
retain the disposable audit securely and do not distribute that snapshot as source.
Use separate credentials/state for the real-mailbox pilot. Reinitialize OAuth/API
connections in every new runtime; saved processes and connections do not survive.

## Read-only readiness checks before a pilot

```sh
cd /workspace/Gmail-cleanup
.venv/bin/gmail-cleanup auth-check --env-auth
```

This refreshes OAuth in memory and initializes the client, with no mailbox request.
It is not a complete account or API-access check. Before any real-mailbox write:

1. Verify the refresh response's effective granted scopes include both exact URLs.
2. Use `users.getProfile(userId='me')` and Drive `about.get(fields='user')` through
   the direct API client to confirm access and the intended account. Keep identity
   details private; do not print tokens or raw API exceptions.
3. Check the active network policy and actual read calls on all three runtime hosts.
4. Inspect only explicitly allowlisted pilot messages and run the read-only preview.

Missing/expired grants require fresh local consent and a secure binding update.
403 errors can indicate scope, API enablement or account policy; network/TLS failures
require checking the exact host/proxy/CA configuration. Do not weaken verification
or use broader scopes to bypass a failure. See [the pilot operations and recovery
guide](../README.md#create-verify-then-separately-approve-trash).

Official references: [insert](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/insert),
[Trash](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash),
[threads](https://developers.google.com/workspace/gmail/api/guides/threads),
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), and
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
