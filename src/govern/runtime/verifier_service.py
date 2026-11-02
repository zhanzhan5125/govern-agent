"""Bounded, filesystem-authenticated verification service; no public port."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socketserver
import threading
import time
import uuid
from govern.runtime.verifier_engine import IsolatedEngineBackend,validate_payload


class Jobs:
    def __init__(self,backend):
        self.backend=backend;self.lock=threading.Lock();self.values={}
        self.pool=ThreadPoolExecutor(max_workers=2)
    def submit(self,payload):
        validate_payload(payload,self.backend.profiles)
        with self.lock:
            self.values={k:v for k,v in self.values.items() if v['result'] is None or time.monotonic()-v['updated']<600}
            if sum(v['result'] is None for v in self.values.values())>=4:raise ValueError('Verification queue full')
            completed=[k for k,v in self.values.items() if v['result'] is not None]
            for k in completed[:-28]:del self.values[k]
            job=uuid.uuid4().hex
            self.values[job]={'cancel':threading.Event(),'result':None,'updated':time.monotonic()}
        def run():
            try:result=self.backend.execute(payload,self.values[job]['cancel'].is_set)
            except Exception:result={'execution_status':'environment_error','test_status':'not_run','reason':'verification worker failed safely'}
            with self.lock:self.values[job].update(result=result,updated=time.monotonic())
        self.pool.submit(run)
        return job
    def get(self,job,cancel=False):
        with self.lock:
            if job not in self.values:raise KeyError('Unknown verification job')
            if cancel:self.values[job]['cancel'].set()
            return {'job_id':job,'result':self.values[job]['result']}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,value):
        raw=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def dispatch(self):
        try:
            if self.command=='GET' and self.path=='/health':
                return self.reply(200,{'available':self.server.jobs.backend.available(),'backend':'isolated-docker-service'})
            if self.command=='POST' and self.path=='/jobs':
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16_000_000:raise ValueError('Invalid request size')
                payload=json.loads(self.rfile.read(length))
                return self.reply(202,{'job_id':self.server.jobs.submit(payload)})
            if self.command in {'GET','DELETE'} and re.fullmatch(r'/jobs/[0-9a-f]{32}',self.path):
                return self.reply(200,self.server.jobs.get(self.path.split('/')[-1],cancel=self.command=='DELETE'))
            return self.reply(404,{'error':'Unknown verification operation'})
        except (ValueError,TypeError,KeyError):self.reply(400,{'error':'Verification request rejected'})
    do_GET=dispatch
    do_POST=dispatch
    do_DELETE=dispatch


if hasattr(socketserver,'UnixStreamServer'):
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
else:
    Server=None


def main():
    if Server is None:raise RuntimeError('The verifier service runs in its Linux container, not on Windows')
    profiles=json.loads(Path(os.environ['GOVERN_VERIFICATION_PROFILES']).read_text())
    socket=Path(os.environ.get('GOVERN_VERIFIER_SOCKET','/run/govern-verifier/verifier.sock'))
    socket.parent.mkdir(parents=True,exist_ok=True)
    if socket.exists():socket.unlink()
    backend=IsolatedEngineBackend(profiles)
    backend.reap_orphans()
    server=Server(str(socket),Handler);server.jobs=Jobs(backend)
    # App UID may connect; root health checks use GID 0 without DAC override.
    socket.chmod(0o660);os.chown(socket,10001,0)
    server.serve_forever(poll_interval=.2)


if __name__=='__main__':main()
