from pathlib import Path
import tempfile
import unittest
from gmail_cleanup.drive import verify_file,BackupVerificationError
from gmail_cleanup.engine import plan_message,create,trash_original
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import digest
from .helpers import FakeDrive,FakeGmail,Store,message


class DriveStabilityTests(unittest.TestCase):
    def test_final_observed_change_is_never_accepted(self):
        drive=FakeDrive();drive.add('copy',b'Test')
        download=drive.download_hash
        def change(file_id):
            result=download(file_id)
            drive.files[file_id]['version']=str(int(drive.files[file_id]['version'])+1)
            if len(drive.downloads)==2:drive.payloads[file_id]=b'evil'
            return result
        drive.download_hash=change
        with self.assertRaises(BackupVerificationError) as caught:verify_file(drive,'copy',digest(b'Test'),4)
        self.assertFalse(caught.exception.observation['verified'])
        self.assertEqual(len(caught.exception.observation['attempts']),2)
        self.assertFalse(caught.exception.observation['attempts'][-1]['checks']['unchanged_version_across_complete_download'])

    def test_late_change_before_trash_results_in_zero_trash_requests(self):
        original,manifest=message()
        api=FakeGmail({'abc123':original})
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite')
            try:
                plan_message(api,store,manifest,'abc123')
                record=create(api,store,manifest,'abc123',execute=True)
                before=len(api.drive.downloads)
                download=api.drive.download_hash
                def late(file_id):
                    value=download(file_id)
                    api.drive.files[file_id]['version']=str(int(api.drive.files[file_id]['version'])+1)
                    if len(api.drive.downloads)-before==2:
                        api.drive.payloads[file_id]=b'X'*len(api.drive.payloads[file_id])
                    return value
                api.drive.download_hash=late
                with self.assertRaises(CleanupError):trash_original(api,store,manifest,'abc123',execute=True,
                    enable_trash=True,approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
                self.assertEqual(api.trashes,[])
                self.assertEqual(api.messages['abc123'],original)
            finally:store.close()

    def test_missing_checksum_is_allowed_only_with_stable_version_and_download(self):
        drive=FakeDrive();drive.add('copy',b'Test');del drive.files['copy']['sha256Checksum']
        proof=verify_file(drive,'copy',digest(b'Test'),4)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['metadata_before']['version'],proof['metadata_after']['version'])
        for malformed in (None,'','0','-1','1.0',True):
            drive.files['copy']['version']=malformed
            with self.subTest(version=malformed),self.assertRaises(BackupVerificationError):verify_file(drive,'copy',digest(b'Test'),4)

    def test_actual_sdk_download_chunks_cannot_hide_same_size_corruption(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        from googleapiclient.discovery import build
        from gmail_cleanup.transport import authorized_transport
        from gmail_cleanup.drive import Drive
        from .test_transport_faults import FakeCredentials
        source=b'A'*(1024*1024)+b'B'*17
        chunks=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                start,end=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
                end=min(end,len(source)-1)
                payload=source[start:end+1] if start==0 else b'X'*(end-start+1)
                chunks.append((start,end))
                self.send_response(206);self.send_header('Content-Range',f'bytes {start}-{end}/{len(source)}')
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            http=authorized_transport(FakeCredentials(),timeout=2,proxy_info=None)
            sdk=build('drive','v3',http=http,static_discovery=True,cache_discovery=False)
            request=sdk.files().get_media(fileId='copy');request.uri=f'http://127.0.0.1:{server.server_port}/media'
            from unittest.mock import MagicMock
            adapter=Drive.__new__(Drive);adapter.service=MagicMock()
            adapter.service.files.return_value.get_media.return_value=request
            fake=FakeDrive();fake.add('copy',source)
            fake.download_hash=adapter.download_hash
            with self.assertRaises(BackupVerificationError) as caught:verify_file(fake,'copy',digest(source),len(source))
            self.assertFalse(caught.exception.observation['checks']['downloaded_sha256_matches_source'])
            self.assertEqual(len(chunks),2)
        finally:
            server.shutdown();server.server_close();thread.join(2)
