import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from gmail_cleanup.api import Gmail, MODIFY_SCOPE, READ_SCOPE, DRIVE_SCOPE, authenticate, decode_raw
from gmail_cleanup.cli import main
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.manifest import load_manifest
from gmail_cleanup.storage import write_private_json
from .helpers import Store
from gmail_cleanup.synthetic import generate


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.api = Gmail.__new__(Gmail)
        self.api.service = Mock()
        self.api.can_modify = True
        self.messages = self.api.service.users.return_value.messages.return_value

    def test_insert_uses_direct_mailbox_insert_labels_thread_dateheader_and_zero_retries(self):
        self.messages.insert.return_value.execute.return_value = {"id": "replacement"}
        result = self.api.insert(b"synthetic raw bytes", ["SENT", "STARRED"], "thread")
        kwargs = self.messages.insert.call_args.kwargs
        self.assertEqual(kwargs["userId"], "me")
        self.assertEqual(kwargs["internalDateSource"], "dateHeader")
        self.assertEqual(kwargs["body"]["labelIds"], ["SENT", "STARRED"])
        self.assertEqual(kwargs["body"]["threadId"], "thread")
        self.assertEqual(decode_raw(kwargs["body"]["raw"]), b"synthetic raw bytes")
        self.assertNotIn("internalDate", kwargs["body"])
        self.assertNotIn("deleted", kwargs)
        self.messages.insert.return_value.execute.assert_called_once_with(num_retries=0)
        self.messages.send.assert_not_called()
        self.messages.delete.assert_not_called()
        self.messages.modify.assert_not_called()
        self.assertEqual(result["id"], "replacement")

    def test_get_fetches_raw_and_metadata_and_never_logs_payload(self):
        self.messages.get.return_value.execute.return_value = {
            "id": "original", "raw": base64.urlsafe_b64encode(b"synthetic bytes").decode().rstrip("=")}
        result = self.api.get("original")
        self.assertEqual(result["raw_bytes"], b"synthetic bytes")
        self.assertNotIn("raw", result)
        self.assertEqual(self.messages.get.call_args.kwargs["format"], "raw")

    def test_write_capability_gate(self):
        self.api.can_modify = False
        with self.assertRaises(CleanupError):
            self.api.insert(b"synthetic", [], "thread")
        with self.assertRaises(CleanupError):
            self.api.trash("original")
        self.messages.insert.assert_not_called()
        self.messages.trash.assert_not_called()

    def test_api_errors_do_not_leak_email_or_token_contents(self):
        self.messages.insert.return_value.execute.side_effect = ValueError("RAW-EMAIL-CONTENT SECRET-TOKEN")
        with self.assertRaises(CleanupError) as error:
            self.api.insert(b"synthetic", [], "thread")
        self.assertNotIn("RAW-EMAIL", str(error.exception))
        self.assertNotIn("TOKEN", str(error.exception))

    def test_invalid_base64_is_rejected(self):
        with self.assertRaises(CleanupError):
            decode_raw("not%%%base64")

    def test_missing_raw_or_malformed_response_is_a_safe_failure(self):
        self.messages.get.return_value.execute.return_value = {"id": "original"}
        with self.assertRaises(CleanupError):
            self.api.get("original")
        self.messages.get.return_value.execute.return_value = None
        with self.assertRaises(CleanupError):
            self.api.get("original")


class OAuthTests(unittest.TestCase):
    def test_user_invoked_oauth_requests_only_the_chosen_scope_and_protects_token(self):
        for modify, expected in ((False, READ_SCOPE), (True, MODIFY_SCOPE)):
            with self.subTest(modify=modify), tempfile.TemporaryDirectory() as temp:
                flow_module = types.ModuleType("google_auth_oauthlib.flow")
                flow_module.InstalledAppFlow = Mock()
                flow = flow_module.InstalledAppFlow.from_client_secrets_file.return_value
                flow.run_local_server.return_value.to_json.return_value = json.dumps({"scopes": [expected]})
                token = Path(temp) / "token.json"
                with patch.dict("sys.modules", {"google_auth_oauthlib.flow": flow_module}):
                    authenticate("user-owned-desktop.json", token, modify=modify)
                flow_module.InstalledAppFlow.from_client_secrets_file.assert_called_once_with(
                    "user-owned-desktop.json", [expected])
                kwargs = flow.run_local_server.call_args.kwargs
                self.assertNotIn("{url}", kwargs["authorization_prompt_message"])
                self.assertEqual(kwargs["timeout_seconds"], 180)
                self.assertEqual(kwargs["prompt"], "consent")
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(token.read_text())["scopes"], [expected])

    def test_existing_token_is_refused_before_oauth_or_any_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "token.json"
            token.write_text("preserved local file")
            with self.assertRaises(CleanupError):
                authenticate("unused-credentials.json", token)
            self.assertEqual(token.read_text(), "preserved local file")

    def test_desktop_grant_without_refresh_token_is_not_saved_as_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            module = types.ModuleType("google_auth_oauthlib.flow")
            module.InstalledAppFlow = Mock()
            flow = module.InstalledAppFlow.from_client_secrets_file.return_value
            flow.run_local_server.return_value.refresh_token = None
            token = Path(temp) / "token.json"
            with patch.dict("sys.modules", {"google_auth_oauthlib.flow": module}), self.assertRaises(CleanupError):
                authenticate("user-owned-desktop.json", token)
            self.assertFalse(token.exists())


class EnvironmentOAuthTests(unittest.TestCase):
    def setUp(self):
        self.variables = {"GMAIL_CLIENT_ID": "synthetic-client-id",
                          "GMAIL_CLIENT_SECRET": "synthetic-proxy-client-binding",
                          "GMAIL_REFRESH_TOKEN": "synthetic-proxy-refresh-binding",
                          "GMAIL_OAUTH_SCOPE": MODIFY_SCOPE}
        self.credentials_module = types.ModuleType("google.oauth2.credentials")
        self.credentials_module.Credentials = Mock()
        self.credentials = self.credentials_module.Credentials.return_value
        self.credentials.valid = True
        self.credentials.granted_scopes = [MODIFY_SCOPE]
        self.transport_module = types.ModuleType("google.auth.transport.requests")
        self.transport_module.Request = Mock()
        self.modules = {"google.oauth2.credentials": self.credentials_module,
                        "google.auth.transport.requests": self.transport_module}

    def test_environment_credentials_refresh_only_at_fixed_endpoint_and_never_write_secrets(self):
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules), \
                patch.object(Gmail, "_configure") as configure, \
                patch("gmail_cleanup.api.write_private_json", side_effect=AssertionError("Do not save proxy placeholders")):
            api = Gmail.from_environment(require_modify=True)
        kwargs = self.credentials_module.Credentials.call_args.kwargs
        self.assertEqual(kwargs["token_uri"], "https://oauth2.googleapis.com/token")
        self.assertEqual(kwargs["scopes"], [MODIFY_SCOPE])
        self.assertIsNone(kwargs["token"])
        self.assertEqual(kwargs["refresh_token"], self.variables["GMAIL_REFRESH_TOKEN"])
        self.assertEqual(kwargs["client_secret"], self.variables["GMAIL_CLIENT_SECRET"])
        self.credentials.refresh.assert_called_once_with(self.transport_module.Request.return_value)
        configure.assert_called_once_with(self.credentials, True)
        self.assertIsInstance(api, Gmail)

    def test_missing_binding_rejected_before_refresh(self):
        for name in self.variables:
            variables = dict(self.variables)
            del variables[name]
            with self.subTest(name=name), patch.dict(os.environ, variables, clear=True), self.assertRaises(CleanupError):
                Gmail.from_environment()
        self.credentials.refresh.assert_not_called()

    def test_readonly_environment_grant_cannot_enable_writes(self):
        self.variables["GMAIL_OAUTH_SCOPE"] = READ_SCOPE
        with patch.dict(os.environ, self.variables, clear=True), self.assertRaises(CleanupError):
            Gmail.from_environment(require_modify=True)
        self.credentials.refresh.assert_not_called()

    def test_readonly_environment_grant_works_without_enabling_writes(self):
        self.variables["GMAIL_OAUTH_SCOPE"] = READ_SCOPE
        self.credentials.granted_scopes = [READ_SCOPE]
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules), \
                patch.object(Gmail, "_configure") as configure:
            Gmail.from_environment()
        self.assertEqual(self.credentials_module.Credentials.call_args.kwargs["scopes"], [READ_SCOPE])
        configure.assert_called_once_with(self.credentials, False)

    def test_invalid_scope_rejected_before_refresh(self):
        self.variables["GMAIL_OAUTH_SCOPE"] = "https://www.googleapis.com/auth/gmail.send"
        with patch.dict(os.environ, self.variables, clear=True), self.assertRaises(CleanupError):
            Gmail.from_environment()
        self.credentials.refresh.assert_not_called()

    def test_modify_grant_enables_only_explicit_mailbox_writes(self):
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules), \
                patch.object(Gmail, "_configure") as configure:
            Gmail.from_environment(require_modify=True)
        configure.assert_called_once_with(self.credentials, True)

    def test_combined_gmail_drive_grant_refreshes_without_enabling_gmail_writes(self):
        self.variables["GMAIL_OAUTH_SCOPE"] = MODIFY_SCOPE + " " + DRIVE_SCOPE
        self.credentials.granted_scopes = [MODIFY_SCOPE, DRIVE_SCOPE]
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules), \
                patch.object(Gmail, "_configure") as configure:
            Gmail.from_environment()
        self.assertEqual(self.credentials_module.Credentials.call_args.kwargs["scopes"], [MODIFY_SCOPE, DRIVE_SCOPE])
        configure.assert_called_once_with(self.credentials, False)

    def test_configured_drive_scope_cannot_upgrade_an_existing_gmail_only_grant(self):
        self.variables["GMAIL_OAUTH_SCOPE"] = MODIFY_SCOPE + " " + DRIVE_SCOPE
        self.credentials.granted_scopes = [MODIFY_SCOPE]
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules), \
                patch.object(Gmail, "_configure") as configure, self.assertRaises(CleanupError):
            Gmail.from_environment()
        configure.assert_not_called()

    def test_returned_scope_or_invalid_token_cannot_claim_readiness(self):
        for granted, valid in (([READ_SCOPE], True), ([MODIFY_SCOPE], False)):
            self.credentials.granted_scopes, self.credentials.valid = granted, valid
            with self.subTest(granted=granted, valid=valid), patch.dict(os.environ, self.variables, clear=True), \
                    patch.dict("sys.modules", self.modules), self.assertRaises(CleanupError):
                Gmail.from_environment()

    def test_refresh_errors_are_sanitized(self):
        self.credentials.refresh.side_effect = ValueError(self.variables["GMAIL_REFRESH_TOKEN"])
        with patch.dict(os.environ, self.variables, clear=True), patch.dict("sys.modules", self.modules):
            with self.assertRaises(CleanupError) as error:
                Gmail.from_environment()
        self.assertNotIn(self.variables["GMAIL_REFRESH_TOKEN"], str(error.exception))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "synthetic"
        generate(self.directory)
        self.manifest = self.directory / "offline-selections.json"
        self.sources = self.directory / "offline-sources.json"
        self.state = self.root / "state.sqlite"
        self.common = ["--manifest", str(self.manifest), "--message-id", "offlineplain", "--state", str(self.state)]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        text = output.getvalue()
        self.assertNotIn("Synthetic correspondence", text)
        self.assertNotIn("sender@example.invalid", text)
        self.assertNotIn("drive.google.com", text)
        return code, json.loads(text)

    def plan(self):
        output = self.root / "plan.json"
        from gmail_cleanup.api import OfflineMailbox
        code, result = self.call(["plan", *self.common, "--offline-sources", str(self.sources), "--output", str(output),
                                 '--initialize-state', '--mailbox-key', OfflineMailbox(self.sources).mailbox_identity()])
        self.assertEqual(code, 0)
        self.assertTrue(output.exists())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        return result

    def test_offline_plan_default_create_and_status_are_runnable(self):
        self.plan()
        with patch("gmail_cleanup.cli.Gmail", side_effect=AssertionError("No network permitted")):
            code, result = self.call(["create", *self.common])
            self.assertEqual(code, 0)
            self.assertTrue(result["dry_run"])
            self.assertIsNone(result["replacement_id"])
            code, status = self.call(["status", *self.common])
            self.assertEqual(code, 0)
            self.assertEqual(status["state"], "planned")

    def test_environment_auth_check_initializes_client_without_any_mailbox_request(self):
        with patch("gmail_cleanup.cli.Gmail.from_environment") as factory:
            code, result = self.call(["auth-check", "--env-auth"])
        self.assertEqual(code, 0)
        self.assertEqual(result["mailbox_requests"], 0)
        factory.assert_called_once_with(require_modify=False)
        self.assertEqual(factory.return_value.mock_calls, [])

    def test_dry_run_with_env_flag_does_not_initialize_credentials(self):
        self.plan()
        with patch("gmail_cleanup.cli.Gmail.from_environment", side_effect=AssertionError("No OAuth refresh permitted")):
            code, result = self.call(["create", *self.common, "--env-auth"])
        self.assertEqual(code, 0)
        self.assertTrue(result["dry_run"])

    def test_explicit_execute_with_offline_sources_cannot_write(self):
        self.plan()
        code, result = self.call(["create", *self.common, "--offline-sources", str(self.sources), "--execute"])
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "planned")
        self.assertIsNone(result["replacement_id"])

    def test_trash_gates_are_checked_before_authentication(self):
        self.plan()
        with patch("gmail_cleanup.cli.Gmail", side_effect=AssertionError("No network permitted")):
            code, result = self.call(["trash", *self.common, "--token", "unused", "--execute"])
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "planned")

    def test_unknown_message_id_never_fetches_mail(self):
        common = ["--manifest", str(self.manifest), "--message-id", "outside", "--state", str(self.state)]
        with patch("gmail_cleanup.cli.Gmail", side_effect=AssertionError("No network permitted")):
            code, result = self.call(["plan", *common, "--token", "unused", "--output", str(self.root / "plan.json")])
        self.assertEqual(code, 2)
        self.assertIn("allowlist", result["error"])

    def test_inspect_can_start_from_empty_selections(self):
        manifest = json.loads(self.manifest.read_text())
        for key in manifest["messages"]:
            manifest["messages"][key] = []
        write_private_json(self.manifest, manifest)
        output = self.root / "inventory.json"
        code, result = self.call(["inspect", *self.common, "--offline-sources", str(self.sources), "--output", str(output)])
        self.assertEqual(code, 0)
        inventory = json.loads(output.read_text())
        self.assertEqual(inventory["parts"][-1]["part"], "0.1")
        self.assertEqual(inventory["parts"][-1]["filename"], "remove.bin")
        with self.assertRaises(CleanupError):
            load_manifest(self.manifest)  # Empty selections never authorize replacement.

    def test_manifest_rejects_implicit_or_ambiguous_selection(self):
        manifest = json.loads(self.manifest.read_text())
        original = json.loads(self.manifest.read_text())
        mutations = [lambda d: d.update(allowlist=[]), lambda d: d["allowlist"].append(d["allowlist"][0]),
                     lambda d: d["messages"]["offlineplain"][0].update(filename="filename-only"),
                     lambda d: d["messages"]["offlineplain"][0].update(decoded_sha256="wrong")]
        for mutate in mutations:
            manifest = json.loads(json.dumps(original))
            mutate(manifest)
            write_private_json(self.manifest, manifest)
            with self.assertRaises(CleanupError):
                load_manifest(self.manifest)

    def test_private_output_refuses_final_symlink(self):
        victim = self.root / "victim.json"
        victim.write_text("preserved")
        link = self.root / "link.json"
        link.symlink_to(victim)
        with self.assertRaises(CleanupError):
            write_private_json(link, {"should": "not overwrite"})
        self.assertEqual(victim.read_text(), "preserved")

    def test_state_refuses_world_readable_existing_database(self):
        self.state.touch(mode=0o644)
        self.state.chmod(0o644)
        with self.assertRaises(CleanupError):
            Store(self.state)
