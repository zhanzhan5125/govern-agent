"""Real Docker smoke using owned fixtures and a disposable DB, never an LLM."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
from govern.runtime.artifacts import ArtifactStore
from govern.runtime.verification import VerificationGateway,ExecutionProfile
from govern.runtime.verifier_client import RemoteVerificationBackend
from govern.storage.store import TaskStore


def smoke():
    profiles=json.loads(Path(os.environ.get('GOVERN_SMOKE_PROFILES','/var/lib/govern/verification-profiles.json')).read_text())
    key,values=next((k,v) for k,v in profiles.items() if v['runner']=='pytest')
    profile=ExecutionProfile(**values)
    backend=RemoteVerificationBackend(os.environ['GOVERN_VERIFIER_SOCKET'])
    assert backend.available(),'independent verifier unavailable'
    source='from time import time as now\ndef fee(amount):\n    assert now() >= 0\n    return amount\n'
    tests='''import os
from pathlib import Path
import billing
def test_fee_and_isolation(monkeypatch):
    monkeypatch.setattr(billing, 'now', lambda: 100)
    assert os.geteuid() == 65534
    assert os.statvfs('/payload').f_flag & os.ST_RDONLY
    assert os.statvfs('/').f_flag & os.ST_RDONLY
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('/var/lib/govern/config.json').exists()
    assert not any(line.split()[1]=='00000000' for line in Path('/proc/net/route').read_text().splitlines()[1:])
    assert not any(k in os.environ for k in ['EVOAGENT_LLM_API_KEY','GITHUB_TOKEN','GOVERN_VERIFIER_SOCKET'])
    assert billing.fee(7) == 7
'''
    with tempfile.TemporaryDirectory(prefix='govern-verifier-smoke-') as directory:
        store=TaskStore(str(Path(directory)/'isolated.sqlite'))
        store.create('smoke','owned-fixture',None,{'verification_smoke':True},'smoke')
        artifacts=ArtifactStore(store)
        snapshot={'snapshot_id':'snap-owned-smoke','base_files':{'billing.py':source},'head_files':{'billing.py':source.replace('return amount','return amount * 2')}}
        snapshot_ref=artifacts.put('smoke','smoke','code-snapshot',snapshot).uri
        plan={'hypothesis':'head doubles the fee','assertion_source':'owned deterministic contract: fee(7)==7',
            'target_symbols':['billing.fee'],'mocked_symbols':['billing.now'],'selectors':['.govern-tests/test_fee.py']}
        args={'finding_id':'owned-fixture','snapshot_id':snapshot['snapshot_id'],'execution_profile':key,'compare':'base_and_head',
              'test_plan_ref':artifacts.put('smoke','smoke','verification-plan',plan).uri,
              'test_bundle_ref':artifacts.put('smoke','smoke','verification-tests',{'test_fee.py':tests}).uri}
        result=VerificationGateway(artifacts,{key:profile},backend).run('smoke','smoke','validator',snapshot_ref,args)
        assert result['execution_status']=='completed',result
        assert result['base']['test_status']=='passed',result['base']
        assert result['head']['test_status']=='failed',result['head']
        assert all(result[s]['cleanup']=='removed' for s in ('base','head')),result
        saved=artifacts.get(result['artifact_ref'].removeprefix('artifact://'),'smoke')
        assert saved['head']['test_status']=='failed'
        summary={'base':result['base']['test_status'],'head':result['head']['test_status'],
            'artifact_roundtrip':True,'external_clock_mock_only':True,'isolation_assertions_passed_in_base':True}
    for name,bundle,expected in (
        ('zero_tests',{'test_empty.py':'value=1\n'},'not_collected'),
        ('missing_dependency',{'test_missing.py':'import govern_nonexistent_dependency_for_smoke\n'},'not_run')):
        result=backend.execute({'billing.py':source},bundle,['.govern-tests/'+next(iter(bundle))],profile)
        assert result['test_status']==expected,result
        assert result['cleanup']=='removed',result
        summary[name]=result['test_status']
    sleep={'test_wait.py':'import time\ndef test_wait(): time.sleep(30)\n'}
    result=backend.execute({},sleep,['.govern-tests/test_wait.py'],replace(profile,timeout=1))
    assert result['execution_status']=='timeout' and result['cleanup']=='removed',result
    summary['timeout']=result['execution_status']
    started=time.monotonic()
    result=backend.execute({},sleep,['.govern-tests/test_wait.py'],profile,lambda:time.monotonic()-started>1)
    assert result['execution_status']=='cancelled' and result['cleanup']=='removed',result
    summary['cancel']=result['execution_status']
    return summary


if __name__=='__main__':print(json.dumps(smoke(),ensure_ascii=False))
