"""Private Unix-socket protocol; the product never receives Docker authority."""
import http.client
import json
import os
import socket
import time
from dataclasses import asdict


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__('localhost',timeout=timeout);self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout);self.sock.connect(self.path)


def request(path, method, route, payload=None):
    connection=UnixConnection(path)
    try:
        body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
        connection.request(method,route,body,{'Content-Type':'application/json'})
        response=connection.getresponse();raw=response.read(2_000_001)
        if len(raw)>2_000_000 or response.status>=400:raise ValueError('Verification service rejected request (HTTP %d)'%response.status)
        return json.loads(raw)
    finally:connection.close()


class RemoteVerificationBackend:
    def __init__(self,path):self.path=path
    def available(self):
        try:return request(self.path,'GET','/health').get('available') is True
        except (OSError,ValueError,http.client.HTTPException):return False
    def execute(self,files,bundle,selectors,profile,cancelled=lambda:False):
        job=None;finished=False
        try:
            if cancelled():return {'execution_status':'cancelled','test_status':'not_run'}
            job=request(self.path,'POST','/jobs',{'files':files,'bundle':bundle,'selectors':selectors,'profile':asdict(profile)})['job_id']
            deadline=time.monotonic()+profile.timeout+45
            while time.monotonic()<deadline:
                if cancelled():
                    request(self.path,'DELETE','/jobs/'+job)
                state=request(self.path,'GET','/jobs/'+job)
                if state.get('result') is not None:finished=True;return state['result']
                time.sleep(.15)
            return {'execution_status':'timeout','test_status':'not_run','reason':'verification service deadline exceeded'}
        except (OSError,ValueError,KeyError,http.client.HTTPException):
            return {'execution_status':'unavailable','test_status':'not_run','reason':'isolated verification service unavailable; host fallback forbidden'}
        finally:
            if job and not finished:
                try:request(self.path,'DELETE','/jobs/'+job)
                except (OSError,ValueError,http.client.HTTPException):pass


def configured_backend():
    path=os.environ.get('GOVERN_VERIFIER_SOCKET')
    if path:return RemoteVerificationBackend(path)
    from govern.runtime.verification import DockerBackend
    return DockerBackend()
