# User-owned Google Cloud and OAuth setup

This guide describes future direct Gmail/Drive API setup, not authorization to
perform it. Reviewer remediation is local-only; the new policy requires independent
re-review before a separately authorized disposable pilot. Historical live evidence
predates this revision. A later real-mailbox pilot requires additional explicit
authorization, account checks and independently retained original backups.
Read [MAINTAINER.md](../MAINTAINER.md) and [RECOVERY.md](RECOVERY.md) first.

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

Use Linux/Python 3.12 for this reviewed release. Store credential files privately
outside git (or `.private/`) with permissions 600 in owned directories. Install the
complete exact `requirements.lock`, then the project without dependency resolution
or isolated build, following [README.md](../README.md). Do not install an unlocked
SDK/auth stack: transport safety depends on the reviewed versions.

With separate explicit consent authorization on the computer running your browser:

```sh
gmail-cleanup auth --credentials .private/gmail-oauth-desktop.json \
  --token .private/gmail-drive-oauth-token.json --modify --drive
```

The command uses the locked desktop OAuth library and atomic private exclusive token
publication. It refuses aliases between credentials and token output before consent.
Never run consent, token refresh or account checks as part of offline remediation.

Choose the intended account deliberately and verify the app/project and both scopes
in the consent screen. The token belongs to that account, not necessarily the
project owner. Use a fresh token filename when repeating consent; commands refuse
overwriting credential files. This release is Linux-only; Windows and other platforms are not declared supported.

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

## Exact runtime hosts and future environment setup

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

Only when separately authorized, review/save environment settings and use the product's **Publish** action. This remediation task does not publish or update live settings.
Draft saving does not publish the environment. The available agent tools save
installation/startup instructions and host configuration; publication is a product
UI action. A snapshot can contain ignored `.private/` files as well as source code:
retain the disposable audit securely and do not distribute that snapshot as source.
Use separate credentials/state for a separately authorized pilot; independently retain
its journal outside the database/environment restore unit, and do not reset state. Reinitialize OAuth/API
connections in every new runtime; saved processes and connections do not survive.

## Future read-only readiness checks (separate authorization required)

```sh
cd /workspace/Gmail-cleanup
.venv/bin/gmail-cleanup auth-check --env-auth
```

This refreshes OAuth in memory and initializes the client, with no mailbox request.
It is not a complete account or API-access check. Before a future independently reviewed disposable pilot, and again before any separately
authorized real-mailbox write:

1. Verify the refresh response's effective granted scopes include both exact URLs.
2. Use `users.getProfile(userId='me')` and Drive `about.get(fields='user')` through
   the direct API client to confirm access and the intended account. Keep identity
   details private; do not print tokens or raw API exceptions.
3. Check the active network policy and actual read calls on all three runtime hosts.
4. Independently retain/verify original `.eml` and attachment backups. Inspect only
   explicitly allowlisted supported messages; confirm zero predicted ordering delta.
5. Bind new state/journal to the SHA-256 of the Gmail profile address after strip/
   casefold/UTF-8. Use `--initialize-state --mailbox-key EXPECTED_KEY` only once, with
   `--journal` outside the database restore unit. Missing history is a stop, not
   permission to initialize again. One operator and no concurrent Drive writers.
6. Run read-only preview, then separately authorize upload, replacement creation
   and exact manual Trash approval. Retain backups beyond the review period.

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
