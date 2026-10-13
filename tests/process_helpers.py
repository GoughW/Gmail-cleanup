"""File-backed fake remote state lives outside the database restore unit."""
import base64
import json
from pathlib import Path
from gmail_cleanup.storage import write_private_json
from .helpers import FakeGmail, FakeDrive, Store


class RemoteDrive(FakeDrive):
    def __init__(self, path, checkpoint):
        super().__init__()
        self.path, self.checkpoint = Path(path), checkpoint
        data=json.loads(self.path.read_text())['drive']
        self.files=data['files']
        self.payloads={k:base64.b64decode(v) for k,v in data['payloads'].items()}
        self.allocated=data['allocated']
        self.creates=data['creates']

    def persist(self):
        data=json.loads(self.path.read_text())
        data['drive']={'files':self.files,'payloads':{k:base64.b64encode(v).decode() for k,v in self.payloads.items()},
                       'allocated':self.allocated,'creates':self.creates}
        write_private_json(self.path,data,authoritative=True)

    def allocate_id(self):
        self.checkpoint('before-allocation')
        result=super().allocate_id()
        self.persist()
        self.checkpoint('after-allocation')
        return result

    def create(self,*args,**kwargs):
        self.checkpoint('before-upload')
        result=super().create(*args,**kwargs)
        self.persist()
        self.checkpoint('after-upload')
        return result

    def download_hash(self,file_id):
        self.checkpoint('before-drive-download')
        return super().download_hash(file_id)


class RemoteGmail(FakeGmail):
    def __init__(self,path,checkpoint=lambda name:None):
        data=json.loads(Path(path).read_text())
        messages={k:{**v,'raw_bytes':base64.b64decode(v['raw_bytes'])} for k,v in data['messages'].items()}
        super().__init__(messages)
        self.path,self.checkpoint=Path(path),checkpoint
        self.inserts=data['inserts']
        self.trashes=data['trashes']
        self.drive=RemoteDrive(path,checkpoint)

    def persist(self):
        data=json.loads(self.path.read_text())
        data.update(messages={k:{**v,'raw_bytes':base64.b64encode(v['raw_bytes']).decode()} for k,v in self.messages.items()},
                    inserts=self.inserts,trashes=self.trashes)
        write_private_json(self.path,data,authoritative=True)

    def insert(self,*args,**kwargs):
        self.checkpoint('before-insert')
        result=super().insert(*args,**kwargs)
        self.persist()
        self.checkpoint('after-insert')
        return result

    def trash(self,*args,**kwargs):
        self.checkpoint('before-trash')
        result=super().trash(*args,**kwargs)
        self.persist()
        self.checkpoint('after-trash')
        return result


def seed(path, original, empty_drive=False):
    fake=FakeGmail({'abc123':original})
    drive=FakeDrive() if empty_drive else fake.drive
    write_private_json(path,{'messages':{k:{**v,'raw_bytes':base64.b64encode(v['raw_bytes']).decode()} for k,v in fake.messages.items()},
        'inserts':[],'trashes':[], 'drive':{'files':drive.files,'payloads':{k:base64.b64encode(v).decode() for k,v in drive.payloads.items()},
                                          'allocated':[],'creates':[]}})


def worker(state,remote,manifest,mode,phase,pipe):
    from gmail_cleanup.engine import create,trash_original
    from gmail_cleanup.preflight import run
    fired=False
    def checkpoint(name):
        nonlocal fired
        if name==phase and not fired:
            fired=True
            pipe.send(name)
            pipe.recv()  # Parent kills this process at an exact observed boundary.
    api=RemoteGmail(remote,checkpoint)
    store=Store(state)
    original_save=store.save
    def save(record,**kwargs):
        stage={'inserted':'id-persist','verified':'replacement-verification',
               'trash_pending':'trash-pending','trashed':'post-trash-verification'}.get(record['state'])
        if stage: checkpoint('before-'+stage)
        original_save(record,**kwargs)
        if stage: checkpoint('after-'+stage)
    store.save=save
    original_intent=store.intent
    def intent(action,binding):
        checkpoint('before-intent')
        value=original_intent(action,binding)
        checkpoint('after-intent')
        return value
    store.intent=intent
    original_append=store.journal.append
    def append(kind,key,value):
        result=original_append(kind,key,value)
        if kind=='intents':checkpoint('after-journal-before-db')
        if kind=='backups' and value.get('state')=='verified':checkpoint('after-drive-verification')
        return result
    store.journal.append=append
    try:
        if mode=='preflight':run(api,store,manifest,api.drive,allow_upload=True,stop_on_failure=True)
        elif mode=='create':create(api,store,manifest,'abc123',execute=True)
        else:
            record=store.get('abc123')
            trash_original(api,store,manifest,'abc123',execute=True,enable_trash=True,
                approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
        pipe.send('unexpected-completion')
    finally:
        store.close()
