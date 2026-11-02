"""Trusted executor: per-attempt volumes transferred by archive, never host binds."""
import io
import json
import re
import tarfile
import time
import uuid
import http.client
from urllib.parse import quote,urlencode
from govern.runtime.verifier_client import UnixConnection
from govern.runtime.verification import PYTEST_RUNNER,NODE_RUNNER,classify
from govern.review.repository import safe_path,sensitive_path
from pathlib import Path

LABEL={'govern.component':'verification-sandbox','govern.executor':'govern-verifier'}


class Engine:
    def __init__(self,path='/var/run/docker.sock'):self.path=path
    def call(self,method,route,payload=None,raw=False):
        connection=UnixConnection(self.path,timeout=20)
        try:
            body=payload if isinstance(payload,bytes) else json.dumps(payload).encode() if payload is not None else None
            connection.request(method,'/v1.45'+route,body,{'Content-Type':'application/x-tar' if isinstance(payload,bytes) else 'application/json'})
            response=connection.getresponse();data=response.read(4_000_001)
            if method=='DELETE' and response.status==404:return {}
            if response.status>=400 or len(data)>4_000_000:raise ValueError('Docker operation failed (HTTP %d)'%response.status)
            return data if raw else json.loads(data) if data else {}
        except http.client.HTTPException:
            raise OSError('Docker transport unavailable') from None
        finally:connection.close()


def validate_payload(payload,profiles):
    if set(payload)!={'files','bundle','selectors','profile'}:raise ValueError('Invalid verification fields')
    profile=payload['profile']
    if not isinstance(profile,dict):raise ValueError('Invalid profile')
    allowed=next((p for p in profiles.values() if all(profile.get(k)==p.get(k) for k in ('image','runner','memory','cpus'))),None)
    if not allowed or set(profile)!={'image','runner','timeout','memory','cpus'}:raise ValueError('Execution profile not authorized')
    timeout=profile['timeout']
    if isinstance(timeout,bool) or not isinstance(timeout,int) or not 1<=timeout<=min(120,allowed.get('timeout',120)):raise ValueError('Invalid verification timeout')
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',profile['image']):raise ValueError('Image must be digest pinned')
    if profile['runner'] not in {'pytest','node'}:raise ValueError('Runner not authorized')
    if not re.fullmatch(r'[1-9][0-9]*m',str(profile['memory'])) or not 64<=int(profile['memory'][:-1])<=2048:raise ValueError('Memory limit must be 64m..2048m')
    if not .1<=float(profile['cpus'])<=2:raise ValueError('CPU limit must be .1..2')
    selectors=payload['selectors']
    if not isinstance(selectors,list) or not selectors or len(selectors)>100 or any(not isinstance(x,str) or x.startswith(('-','/','\\')) or '..' in x or ':' in x.replace('::','') for x in selectors):raise ValueError('Invalid test selectors')
    size=0
    for name in ('files','bundle'):
        files=payload[name]
        if not isinstance(files,dict) or len(files)>2000:raise ValueError('Too many input files')
        normalized=set()
        for path,text in files.items():
            if not isinstance(path,str) or not isinstance(text,str) or len(path)>1024 or not Path(path.replace('\\','/')).parts:raise ValueError('Invalid file')
            safe_path(Path('/input'),path)
            path=path.replace('\\','/')
            if sensitive_path(path) or '.git' in path.split('/') or path in normalized:raise ValueError('Unsafe input path')
            normalized.add(path);size+=len(text.encode())
    if size>12_000_000:raise ValueError('Input size exceeded')
    return profile


def archive(payload):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as tar:
        for directory,values in (('input',payload['files']),('bundle',payload['bundle'])):
            item=tarfile.TarInfo(directory);item.type=tarfile.DIRTYPE;item.mode=0o755;tar.addfile(item)
            for path,text in values.items():
                data=text.encode();item=tarfile.TarInfo(directory+'/'+path.replace('\\','/'));item.size=len(data);item.mode=0o644
                tar.addfile(item,io.BytesIO(data))
        script=PYTEST_RUNNER if payload['profile']['runner']=='pytest' else NODE_RUNNER
        script=script.replace("'/input'","'/payload/input'").replace("'/bundle'","'/payload/bundle'")
        data=script.encode();item=tarfile.TarInfo('runner');item.mode=0o644;item.size=len(data);tar.addfile(item,io.BytesIO(data))
    return output.getvalue()


def decode_logs(data):
    pieces=[];offset=0
    while offset+8<=len(data):
        size=int.from_bytes(data[offset+4:offset+8],'big');offset+=8
        pieces.append(data[offset:offset+size]);offset+=size
    return b''.join(pieces).decode('utf-8','replace')[-100_000:]


class IsolatedEngineBackend:
    def __init__(self,profiles,engine=None):self.profiles=profiles;self.engine=engine or Engine()
    def available(self):
        try:
            if self.engine.call('GET','/info').get('OSType')!='linux':return False
            return any(self.image_available(p['image']) for p in self.profiles.values())
        except (OSError,ValueError):return False
    def image_available(self,image):
        try:self.engine.call('GET','/images/'+quote(image,safe='')+'/json');return True
        except (OSError,ValueError):return False
    def reap_orphans(self):
        filters=json.dumps({'label':['govern.executor=govern-verifier']})
        containers=self.engine.call('GET','/containers/json?'+urlencode({'all':'true','filters':filters}))
        for item in containers:
            if item.get('Labels',{}).get('govern.executor')=='govern-verifier' and any(n.startswith('/govern-verify-') for n in item.get('Names',[])):
                self.engine.call('DELETE','/containers/'+item['Id']+'?force=true')
        volumes=self.engine.call('GET','/volumes?'+urlencode({'filters':filters})).get('Volumes') or []
        for item in volumes:
            if item.get('Labels',{}).get('govern.executor')=='govern-verifier' and item['Name'].startswith('govern-verify-'):
                self.engine.call('DELETE','/volumes/'+item['Name'])
    def execute(self,payload,cancelled=lambda:False):
        profile=validate_payload(payload,self.profiles);engine=self.engine
        name='govern-verify-'+uuid.uuid4().hex;volume=name+'-input';stage=name+'-stage'
        started=time.monotonic();result={'execution_status':'environment_error','test_status':'not_run','sandbox_id':name,
            'image':profile['image'],'network':'none','isolation':'docker-container','input_transport':'archive-per-attempt-volume'}
        try:
            if cancelled():result['execution_status']='cancelled';return result
            engine.call('POST','/volumes/create',{'Name':volume,'Labels':LABEL})
            mount={'Type':'volume','Source':volume,'Target':'/payload','ReadOnly':False}
            engine.call('POST','/containers/create?name='+stage,{'Image':profile['image'],'Entrypoint':['python' if profile['runner']=='pytest' else 'node'],
                'Labels':LABEL,'HostConfig':{'NetworkMode':'none','Mounts':[mount]}})
            # This staging container is never started; no repository code runs here.
            engine.call('PUT','/containers/'+stage+'/archive?path=/payload',archive(payload))
            engine.call('DELETE','/containers/'+stage+'?force=true')
            config={'Image':profile['image'],'Entrypoint':['python' if profile['runner']=='pytest' else 'node'], 'Cmd':['/payload/runner'],
                'User':'65534:65534','WorkingDir':'/work','Labels':LABEL,
                'Env':['HOME=/tmp','PYTHONDONTWRITEBYTECODE=1','GOVERN_TEST_SELECTORS='+json.dumps(payload['selectors'])],
                'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges'],
                    'PidsLimit':128,'Memory':int(profile['memory'].removesuffix('m'))*1024*1024,'NanoCpus':int(float(profile['cpus'])*1e9),
                    'Mounts':[{**mount,'ReadOnly':True}], 'Tmpfs':{'/work':'rw,nosuid,nodev,size=256m,mode=1777','/tmp':'rw,nosuid,nodev,size=64m,mode=1777'},
                    'LogConfig':{'Type':'json-file','Config':{'max-size':'1m','max-file':'1'}}}}
            engine.call('POST','/containers/create?name='+name,config)
            engine.call('POST','/containers/'+name+'/start')
            while True:
                if cancelled():result['execution_status']='cancelled';break
                if time.monotonic()-started>profile['timeout']:result['execution_status']='timeout';break
                state=engine.call('GET','/containers/'+name+'/json')['State']
                if not state['Running']:
                    text=decode_logs(engine.call('GET','/containers/'+name+'/logs?stdout=true&stderr=true&tail=2000',raw=True))
                    records=[line[14:] for line in text.splitlines() if line.startswith('GOVERN_RESULT=')]
                    raw=json.loads(records[-1]) if records else {'report_error':'missing structured test report','exit_code':state.get('ExitCode')}
                    result.update(execution_status='completed' if records else 'environment_error',test_status=classify(raw),observations=raw,logs=text)
                    if state.get('OOMKilled'):result.update(execution_status='environment_error',test_status='not_run',reason='sandbox memory limit exceeded')
                    break
                time.sleep(.1)
        except (OSError,ValueError,KeyError):result['reason']='isolated Docker operation failed; no host fallback'
        finally:
            cleaned=True
            for route in ('/containers/'+name+'?force=true','/containers/'+stage+'?force=true','/volumes/'+volume):
                try:engine.call('DELETE',route)
                except (OSError,ValueError):
                    # An already-removed staging container is expected.
                    if stage not in route:cleaned=False
            result['cleanup']='removed' if cleaned else 'operator_cleanup_required'
            result['duration_ms']=int((time.monotonic()-started)*1000)
        return result
