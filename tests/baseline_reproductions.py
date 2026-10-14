"""Run from an archived b21638b tree with its package/tests first on sys.path.

Uses fake services, temporary files and no credentials. Output is sanitized counts.
The baseline is intentionally unsafe; this is evidence, not the acceptance gate.
"""
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from contextlib import redirect_stdout
import html5lib

from tests.helpers import FakeGmail,message
from gmail_cleanup.mime import parse,parts,transform,body_text,digest
from gmail_cleanup.engine import plan_message,create,trash_original
from gmail_cleanup.storage import write_private_json, Store
from gmail_cleanup.cli import main

evidence={}
def lifecycle(original,manifest):
    api=FakeGmail({'abc123':original})
    with tempfile.TemporaryDirectory() as directory:
        store=Store(Path(directory)/'state.sqlite')
        try:
            plan_message(api,store,manifest,'abc123')
            record=create(api,store,manifest,'abc123',execute=True)
            result=trash_original(api,store,manifest,'abc123',execute=True,enable_trash=True,
                approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
            return api,result
        finally:store.close()

original,manifest=message()
parsed=parse(original['raw_bytes']);parsed.get_payload()[0].set_param('format','flowed');parsed.get_payload()[0].set_param('delsp','yes')
original['raw_bytes']=parsed.as_bytes()
api,result=lifecycle(original,manifest)
evidence['F1']={'flowed_input_trash_requests':len(api.trashes)}

original,manifest=message()
api=FakeGmail({'abc123':original})
with tempfile.TemporaryDirectory() as directory:
    store=Store(Path(directory)/'state.sqlite')
    try:
        plan_message(api,store,manifest,'abc123');record=create(api,store,manifest,'abc123',execute=True)
        download=api.drive.download_hash;count=[0]
        def late(file_id):
            value=download(file_id);count[0]+=1;api.drive.files[file_id]['version']=str(int(api.drive.files[file_id]['version'])+1)
            if count[0]==2:api.drive.payloads[file_id]=b'X'*len(api.drive.payloads[file_id])
            return value
        api.drive.download_hash=late
        result=trash_original(api,store,manifest,'abc123',execute=True,enable_trash=True,
            approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
        evidence['F2']={'late_content_change_trash_requests':len(api.trashes),'postcheck_state':result['state']}
    finally:store.close()

original,manifest=message();parsed=parse(original['raw_bytes'])
dict(parts(parsed))[manifest['messages']['abc123'][0]['part']]['Content-Location']='image.png'
_,removed,_=transform(parsed.as_bytes(),manifest['messages']['abc123'],'baseline')
evidence['F3']={'content_location_resources_removed':len(removed)}

with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);original,manifest=message();api=FakeGmail({'abc123':original});state=root/'state.sqlite'
    choices={'version':2,'allowlist':['abc123'],'messages':{'abc123':[{'part':c['part'],'decoded_sha256':c['decoded_sha256']} for c in manifest['messages']['abc123']]}}
    input_path=root/'manifest.json';write_private_json(input_path,choices)
    with patch('gmail_cleanup.cli.Gmail.from_environment',return_value=api),redirect_stdout(io.StringIO()):
        result=main(['preflight','--env-auth','--manifest',str(input_path),'--state',str(state),'--output',str(state)])
    evidence['F4']={'aliased_cli_exit':result,'database_overwritten_by_json':state.read_bytes().startswith(b'{')}

with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);original,manifest=message();api=FakeGmail({'abc123':original});state=root/'state.sqlite';snapshot=root/'snapshot.sqlite'
    store=Store(state);plan_message(api,store,manifest,'abc123');con=sqlite3.connect(snapshot);store.db.backup(con);con.close()
    api.lose_insert_response=True
    try:create(api,store,manifest,'abc123',execute=True)
    except Exception:pass
    store.close();src=sqlite3.connect(snapshot);dst=sqlite3.connect(state);src.backup(dst);src.close();dst.close()
    api.lose_insert_response=False;store=Store(state)
    try:create(api,store,manifest,'abc123',execute=True)
    finally:store.close()
    evidence['F5']={'insert_requests_after_database_restore':len(api.inserts)}

original,manifest=message('html');parsed=parse(original['raw_bytes'])
next(p for _,p in parts(parsed) if p.get_content_type()=='text/html').set_content('<!-- unfinished',subtype='html')
original['raw_bytes']=parsed.as_bytes();api,result=lifecycle(original,manifest)
replacement=parse(api.messages[result['replacement_id']]['raw_bytes'])
html=next(body_text(p) for _,p in parts(replacement) if p.get_content_type()=='text/html')
tree=html5lib.parse(html,namespaceHTMLElements=False)
evidence['F6']={'visible_note_anchors':len(list(tree.iter('a'))),'trash_requests':len(api.trashes)}

# This isolated baseline reproduction deliberately exercises upstream behavior;
# production transport never alters httplib2 globals.
import httplib2
from google.auth.credentials import AnonymousCredentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
submissions=[]
class ReplayHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        submissions.append(self.rfile.read(int(self.headers['Content-Length'])))
        if len(submissions)==1:
            self.close_connection=True;self.connection.close();return
        body=b'{"id":"fake-replacement"}'
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),ReplayHandler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
old_retries=httplib2.RETRIES
try:
    httplib2.RETRIES=1
    http=AuthorizedHttp(AnonymousCredentials(),http=httplib2.Http(proxy_info=None))
    service=build('gmail','v1',http=http,static_discovery=True,cache_discovery=False)
    request=service.users().messages().insert(userId='me',body={'raw':'ZmFrZQ=='})
    request.uri=f'http://127.0.0.1:{server.server_port}/insert'
    request.execute(num_retries=0)
    evidence['F7']={'accepted_post_submissions':len(submissions),'identical_bodies':submissions[0]==submissions[1],
                    'execute_num_retries':0,'httplib2_RETRIES':1,'httplib2_version':httplib2.__version__}
finally:
    httplib2.RETRIES=old_retries;server.shutdown();server.server_close();thread.join(2)

with tempfile.TemporaryDirectory() as directory:
    target=Path(directory)/'report.json';target.write_text('{"old":true}')
    try:write_private_json(target,{'unserializable':object()})
    except TypeError:pass
    evidence['F8']={'old_artifact_complete_after_serialization_failure':target.read_text()=='{"old":true}',
                     'artifact_bytes_after_failure':target.stat().st_size}
    target.write_text('{"old":true}')
    with patch('gmail_cleanup.storage.json.dump',side_effect=OSError('injected disk full')):
        try:write_private_json(target,{'new':True})
        except OSError:pass
    evidence['F8']['artifact_bytes_after_injected_write_failure']=target.stat().st_size
print(json.dumps(evidence,sort_keys=True))
