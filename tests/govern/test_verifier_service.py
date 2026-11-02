import copy
import io
import json
import os
import tarfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch
from govern.runtime.verifier_engine import validate_payload,archive,IsolatedEngineBackend,decode_logs
from govern.runtime.verifier_service import Jobs
from govern.runtime.verifier_client import RemoteVerificationBackend,configured_backend
from govern.runtime.verification import ExecutionProfile,DockerBackend

PROFILE={'image':'registry/python@sha256:'+'a'*64,'runner':'pytest','timeout':120,'memory':'512m','cpus':'1'}
PROFILES={'python-pytest':PROFILE}
def payload():return {'files':{'billing.py':'def fee(): return 1'},'bundle':{'test_fee.py':'def test_fee(): assert True'},'selectors':['.govern-tests/test_fee.py'],'profile':copy.deepcopy(PROFILE)}
def framed(text):
    data=text.encode();return b'\x01\0\0\0'+len(data).to_bytes(4,'big')+data


class FakeEngine:
    def __init__(self):self.calls=[];self.started=False
    def call(self,method,route,value=None,raw=False):
        self.calls.append((method,route,value))
        if route=='/info':return {'OSType':'linux'}
        if route.startswith('/images/'):return {}
        if route.endswith('/start'):self.started=True
        if route.endswith('/json'):return {'State':{'Running':False,'ExitCode':0}}
        if '/logs?' in route:return framed('GOVERN_RESULT={"exit_code":0,"collected":1,"failures":0,"errors":0}\n')
        return {}


class VerifierServiceTests(TestCase):
    def test_request_cannot_choose_image_mounts_resources_or_host_paths(self):
        for change in [('image','attacker:latest'),('memory','4096m'),('cpus','10'),('runner','shell'),('timeout',121)]:
            body=payload();body['profile'][change[0]]=change[1]
            with self.assertRaises(ValueError):validate_payload(body,PROFILES)
        for path in ('../secret','/etc/passwd','C:\\secret','a/../../b','.env','.git/config',''):
            body=payload();body['files']={path:'x'}
            with self.assertRaises(ValueError):validate_payload(body,PROFILES)
        body=payload();body['mounts']=['/var/run/docker.sock']
        with self.assertRaises(ValueError):validate_payload(body,PROFILES)
        for selector in ('--override-ini=x','/etc/passwd','../test.py'):
            body=payload();body['selectors']=[selector]
            with self.assertRaises(ValueError):validate_payload(body,PROFILES)

    def test_archive_contains_only_relative_regular_files_and_trusted_runner(self):
        with tarfile.open(fileobj=io.BytesIO(archive(payload()))) as tar:
            self.assertEqual({'input','input/billing.py','bundle','bundle/test_fee.py','runner'},set(tar.getnames()))
            self.assertTrue(all(m.isfile() or m.isdir() for m in tar.getmembers()))
            self.assertIn(b'/payload/input',tar.extractfile('runner').read())

    def test_isolated_volume_delivery_and_cleanup_no_bind_no_socket_no_credentials(self):
        engine=FakeEngine();backend=IsolatedEngineBackend(PROFILES,engine)
        result=backend.execute(payload())
        self.assertEqual('passed',result['test_status']);self.assertEqual('removed',result['cleanup'])
        created=[v for m,r,v in engine.calls if m=='POST' and '/containers/create?' in r]
        workload=created[-1];host=workload['HostConfig']
        self.assertEqual('none',host['NetworkMode']);self.assertTrue(host['ReadonlyRootfs'])
        self.assertEqual('65534:65534',workload['User']);self.assertEqual(['ALL'],host['CapDrop'])
        self.assertEqual('volume',host['Mounts'][0]['Type']);self.assertTrue(host['Mounts'][0]['ReadOnly'])
        self.assertNotIn('docker.sock',json.dumps(workload));self.assertNotIn('API_KEY',json.dumps(workload))
        self.assertFalse(any('-stage/start' in r for _,r,_ in engine.calls))
        self.assertTrue(any(m=='DELETE' and '/volumes/' in r for m,r,_ in engine.calls))

    def test_cancel_timeout_and_engine_error_still_cleanup(self):
        engine=FakeEngine();backend=IsolatedEngineBackend(PROFILES,engine)
        result=backend.execute(payload(),lambda:engine.started)
        self.assertEqual('cancelled',result['execution_status']);self.assertEqual('removed',result['cleanup'])
        with patch('govern.runtime.verifier_engine.time.monotonic',side_effect=[0,121,121]):result=backend.execute(payload())
        self.assertEqual('timeout',result['execution_status']);self.assertEqual('not_run',result['test_status'])
        engine.call=lambda *a,**k:(_ for _ in ()).throw(OSError('not exposed'))
        result=backend.execute(payload());self.assertEqual('environment_error',result['execution_status'])
        self.assertNotIn('not exposed',json.dumps(result))

    def test_remote_backend_never_falls_back_and_cancel_is_forwarded(self):
        backend=RemoteVerificationBackend('/private/socket')
        with patch('govern.runtime.verifier_client.request',side_effect=OSError()),patch.object(DockerBackend,'execute',side_effect=AssertionError('host fallback')):
            self.assertFalse(backend.available())
            self.assertEqual('unavailable',backend.execute({}, {},['test.py'],ExecutionProfile(**PROFILE))['execution_status'])
        with patch.dict(os.environ,{'GOVERN_VERIFIER_SOCKET':'/private/socket'}):self.assertIsInstance(configured_backend(),RemoteVerificationBackend)
        replies=[{'job_id':'abc'},{},{'result':{'execution_status':'cancelled','test_status':'not_run'}}]
        checks=iter([False,True])
        with patch('govern.runtime.verifier_client.request',side_effect=replies) as rpc:
            result=backend.execute({}, {},['test.py'],ExecutionProfile(**PROFILE),lambda:next(checks))
            self.assertEqual('cancelled',result['execution_status'])
            self.assertTrue(any(c.args[1]=='DELETE' for c in rpc.call_args_list))

    def test_queue_is_bounded_and_jobs_cancel(self):
        stop=threading.Event()
        class Backend:
            profiles=PROFILES
            def execute(self,body,cancelled):
                while not stop.is_set() and not cancelled():time.sleep(.01)
                return {'execution_status':'cancelled' if cancelled() else 'completed'}
        jobs=Jobs(Backend())
        try:
            ids=[jobs.submit(payload()) for _ in range(4)]
            with self.assertRaises(ValueError):jobs.submit(payload())
            jobs.get(ids[0],True)
        finally:stop.set();jobs.pool.shutdown(wait=True)
        self.assertEqual('cancelled',jobs.get(ids[0])['result']['execution_status'])

    def test_orphan_reaping_is_label_and_name_scoped(self):
        calls=[]
        class Engine:
            def call(self,method,route,*a):
                calls.append((method,route))
                if '/containers/json?' in route:return [{'Id':'ours','Names':['/govern-verify-a'],'Labels':{'govern.executor':'govern-verifier'}},{'Id':'other','Names':['/database'],'Labels':{}}]
                if route.startswith('/volumes?'):return {'Volumes':[{'Name':'govern-verify-a-input','Labels':{'govern.executor':'govern-verifier'}},{'Name':'database-data','Labels':{}}]}
                return {}
        IsolatedEngineBackend(PROFILES,Engine()).reap_orphans()
        self.assertTrue(any('/containers/ours?' in r for m,r in calls if m=='DELETE'))
        self.assertFalse(any('database' in r or 'other?' in r for m,r in calls if m=='DELETE'))
