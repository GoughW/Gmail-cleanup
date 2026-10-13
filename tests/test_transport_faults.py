"""Actual locked SDK/auth/HTTP stack, fake credentials and loopback faults only."""
import datetime
import io
import json
import socket
import struct
import threading
import time
from io import BytesIO
import tempfile
from pathlib import Path
import ssl
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

from google.auth.credentials import Credentials
from googleapiclient.discovery import build
from gmail_cleanup.transport import authorized_transport
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.api import Gmail


class FakeCredentials(Credentials):
    def __init__(self, expired=False):
        super().__init__()
        self.token = "LOCAL-FAKE-TOKEN"
        self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=-1 if expired else 1)
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)


class FaultServer:
    def __init__(self, mode):
        self.mode, self.requests = mode, []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = b'{}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True  # Deliberately stale unadvertised keep-alive.
            def do_POST(self):
                owner.requests.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if mode in {"missing", "reset", "bad-status", "timeout"}:
                    if mode == "bad-status":
                        self.connection.sendall(b"not an HTTP status\r\n\r\n")
                    if mode == "reset":
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    if mode == "timeout":
                        time.sleep(0.15)
                    self.close_connection = True
                    self.connection.close()
                    return
                status = mode if isinstance(mode, int) else 200
                body = b'{"id":"fake-replacement"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + (50 if mode == "partial" else 0)))
                if status in (301, 302, 303, 307, 308):
                    self.send_header("Location", "/redirected")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if mode == "partial":
                    self.close_connection = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mutation"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class TransportFaultTests(unittest.TestCase):
    def request(self, server, credentials=None, transport=None):
        http = transport or authorized_transport(credentials or FakeCredentials(), timeout=0.08, proxy_info=None)
        service = build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True)
        request = service.users().messages().insert(userId="me", body={"raw": "bG9jYWwtZmFrZQ=="})
        request.uri = server.url
        return request

    def test_complete_request_then_transport_fault_is_one_submission(self):
        for mode in ("missing", "bad-status", "reset", "timeout", "partial"):
            with self.subTest(mode=mode):
                server = FaultServer(mode)
                try:
                    with self.assertRaises(CleanupError):
                        Gmail._execute(self.request(server))
                    self.assertEqual(len(server.requests), 1)
                finally:
                    server.close()

    def test_auth_rate_server_and_redirect_responses_never_replay(self):
        for status in (401, 403, 429, 500, 502, 503, 301, 302, 303, 307, 308):
            with self.subTest(status=status):
                server = FaultServer(status)
                credentials = FakeCredentials()
                try:
                    with self.assertRaises(CleanupError) as caught:
                        Gmail._execute(self.request(server, credentials))
                    self.assertEqual(len(server.requests), 1)
                    self.assertEqual(credentials.refreshes, 0)
                    self.assertNotIn("LOCAL-FAKE-TOKEN", str(caught.exception))
                finally:
                    server.close()

    def test_expired_credentials_refresh_before_single_submission(self):
        server = FaultServer(200)
        credentials = FakeCredentials(expired=True)
        try:
            self.assertEqual(Gmail._execute(self.request(server, credentials))["id"], "fake-replacement")
            self.assertEqual(credentials.refreshes, 1)
            self.assertEqual(len(server.requests), 1)
        finally:
            server.close()

    def test_stale_keepalive_does_not_reconnect_and_replay_post(self):
        server = FaultServer(200)
        http = authorized_transport(FakeCredentials(), timeout=0.08, proxy_info=None)
        try:
            http.request(server.url, "GET")
            try:
                Gmail._execute(self.request(server, transport=http))
            except CleanupError:
                pass
            self.assertLessEqual(len(server.requests), 1)
        finally:
            server.close()

    def test_actual_drive_upload_and_gmail_trash_share_no_replay_boundary(self):
        from googleapiclient.http import MediaIoBaseUpload
        for operation in ('upload','trash'):
            with self.subTest(operation=operation):
                server=FaultServer('missing')
                http=authorized_transport(FakeCredentials(),timeout=0.08,proxy_info=None)
                try:
                    if operation=='upload':
                        service=build('drive','v3',http=http,cache_discovery=False,static_discovery=True)
                        request=service.files().create(body={'id':'persisted-fake-id','name':'fake.bin'},
                            media_body=MediaIoBaseUpload(BytesIO(b'fake attachment'),mimetype='application/octet-stream',resumable=False))
                    else:
                        service=build('gmail','v1',http=http,cache_discovery=False,static_discovery=True)
                        request=service.users().messages().trash(userId='me',id='explicit-fake-original')
                    request.uri=server.url
                    with self.assertRaises(CleanupError):Gmail._execute(request)
                    self.assertEqual(len(server.requests),1)
                finally:server.close()

    def test_tls_verification_and_explicit_ca_trust_preserved(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        server=FaultServer(200)
        try:
            with tempfile.TemporaryDirectory() as directory:
                key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
                name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'LOCAL-TEST-CA')])
                now=datetime.datetime.now(datetime.timezone.utc)
                cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
                    .not_valid_after(now+datetime.timedelta(hours=1))
                    .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
                    .sign(key,hashes.SHA256()))
                cert_path=Path(directory)/'ca.pem';key_path=Path(directory)/'key.pem'
                cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert_path,key_path)
                server.server.socket=context.wrap_socket(server.server.socket,server_side=True)
                server.url=server.url.replace('http:','https:')
                with self.assertRaises(CleanupError):Gmail._execute(self.request(server))
                self.assertEqual(server.requests,[])
                http=authorized_transport(FakeCredentials(),timeout=2,proxy_info=None,ca_certs=str(cert_path))
                self.assertEqual(Gmail._execute(self.request(server,transport=http))['id'],'fake-replacement')
                self.assertEqual(len(server.requests),1)
                self.assertFalse(http.http.disable_ssl_certificate_validation)
        finally:server.close()
