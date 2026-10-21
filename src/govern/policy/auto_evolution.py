"""Local single-worker feedback trigger. Never approves or publishes candidates."""
import os
import threading
import time
from collections import Counter
from .feedback_pool import pending_groups
from .feedback_evolution import overrides
from .bundle import compile_bundle, ROLES
from govern.review.repository import digest

DEFAULTS={'enabled':False,'threshold':10,'cooldown_minutes':1440}


def configuration(config):
    return {**DEFAULTS,**config.get('auto_evolution',{})}


def validate(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('invalid auto-evolution configuration')
    result={**DEFAULTS,**value}
    if not isinstance(result['enabled'],bool):raise ValueError('enabled must be boolean')
    for key,low,high in [('threshold',1,100),('cooldown_minutes',1,10080)]:
        v=result[key]
        if isinstance(v,bool) or not isinstance(v,int) or not low<=v<=high:raise ValueError(f'{key} must be an integer in {low}..{high}')
    return result


class AutomaticEvolution:
    def __init__(self, workbench, allowed=None, clock=time.time):
        self.w=workbench;self.allowed=allowed or (lambda:os.environ.get('GOVERN_AUTO_EVOLUTION_MODEL_CALLS')=='1')
        self.clock=clock;self.lock=threading.Lock();self.stop_event=threading.Event();self.wake=threading.Event();self.thread=None

    @property
    def running(self):return self.lock.locked()

    def status(self,tenant):
        state=self.w.store.auto_evolution_state(tenant)
        if not configuration(self.w.config)['enabled']:state={**state,'status':'disabled'}
        return {'config':configuration(self.w.config),'model_calls_authorized':self.allowed(),
                'state':state or {'status':'below_threshold'},'running':self.running,
                'publication':'human_approval_required','deployment':'single_process_worker'}

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self._loop,name='govern-auto-evolution',daemon=True);self.thread.start()

    def close(self):self.stop_event.set();self.wake.set()

    def _loop(self):
        while not self.stop_event.is_set():
            if configuration(self.w.config)['enabled']:
                try:
                    for tenant in self.w.store.auto_evolution_tenants():
                        if self.stop_event.is_set():break
                        self.check(tenant)
                except Exception:
                    # A transient DB failure must not terminate the periodic worker.
                    pass
            self.wake.wait(30);self.wake.clear()

    def check(self,tenant):
        if not self.lock.acquire(blocking=False):return {'status':'busy'}
        try:return self._check(tenant)
        finally:self.lock.release()

    def _check(self,tenant):
        cfg=configuration(self.w.config);store=self.w.store
        previous=store.auto_evolution_state(tenant)
        def save(status,**values):
            state={**previous,'status':status,'checked_at':self.clock(),**values}
            store.auto_evolution_state(tenant,state);return state
        if not cfg['enabled']:return save('disabled')
        pipeline=self.w.evolution
        base=pipeline._baseline(tenant,'skill','govern-review')['content']
        bundle=compile_bundle(overrides(base))
        protected=sum([pipeline.dataset.cases(p) for p in ('train','validation','holdout','shadow')],[])
        groups,pool=pending_groups(store,tenant,ROLES,protected,digest(base),bundle['source_sha256'])
        counts=Counter()
        for key,examples in groups.items():counts[key[0]]+=len(examples)
        stages=[role for role,n in counts.items() if n>=cfg['threshold']]
        metadata={'pending_by_stage':dict(counts),'pool':pool,'eligible_stages':stages}
        if not stages:return save('below_threshold',**metadata)
        if not self.allowed():return save('waiting_authorization',**metadata)
        next_at=previous.get('last_attempt_at',0)+cfg['cooldown_minutes']*60
        if previous.get('last_attempt_at') is not None and self.clock()<next_at:
            return save('cooldown',next_allowed_at=next_at,**metadata)
        if pipeline.generation_lock.locked():return save('manual_generation_running',**metadata)
        attempt=self.clock()
        save('running',last_attempt_at=attempt,candidate_ids=[],**metadata)
        ids=[]
        try:
            if not self.allowed() or not configuration(self.w.config)['enabled']:return save('stopped',last_attempt_at=attempt)
            generated=pipeline.generate(tenant,'auto-feedback-trigger',stages=stages)
            candidates=generated.get('candidates',[]);ids=[c['id'] for c in candidates]
            evaluation=[]
            for c in candidates:
                if self.stop_event.is_set() or not self.allowed() or not configuration(self.w.config)['enabled']:break
                # evaluate runs Validation/Holdout only; no approve/shadow/activate calls.
                try:
                    result=pipeline.evaluate(c['id'],tenant,c['revision'])
                    evaluation.append({'candidate_id':c['id'],'status':result.get('status')})
                except Exception as exc:
                    evaluation.append({'candidate_id':c['id'],'status':'evaluation_error','error_type':type(exc).__name__})
            if self.stop_event.is_set() or not self.allowed() or not configuration(self.w.config)['enabled']:
                status='stopped'
            elif any(e['status']=='evaluation_error' for e in evaluation):status='evaluation_error'
            elif any(e['status']=='REJECTED' for e in evaluation):status='evaluation_rejected'
            elif candidates and all(e['status']=='HOLDOUT_PASSED' for e in evaluation) and len(evaluation)==len(candidates):status='awaiting_human_review'
            else:status='awaiting_evaluation' if candidates else 'no_candidates'
            return save(status,last_attempt_at=attempt,
                        candidate_ids=ids,evaluation=evaluation,**metadata)
        except Exception as exc:
            return save('failed',last_attempt_at=attempt,candidate_ids=ids,error_type=type(exc).__name__,**metadata)
