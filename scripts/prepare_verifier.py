"""Export only execution profiles, never model/Jira/GitHub credentials."""
import json
import os
from pathlib import Path


def prepare(root):
    state=Path(root).resolve()/'.govern'
    profiles=json.loads((state/'config.json').read_text(encoding='utf-8-sig')).get('execution_profiles',{})
    from govern.runtime.verification import ExecutionProfile
    from govern.runtime.verifier_engine import validate_payload
    from dataclasses import asdict
    sanitized={k:asdict(ExecutionProfile(**v)) for k,v in profiles.items()}
    for profile in sanitized.values():validate_payload({'files':{},'bundle':{},'selectors':['tests/test_smoke.py'],'profile':profile},sanitized)
    target=state/'verification-profiles.json';temporary=state/'verification-profiles.tmp'
    temporary.write_text(json.dumps(sanitized,indent=2),encoding='utf-8');temporary.chmod(0o600);os.replace(temporary,target)
    return {'profile_count':len(sanitized),'profile_file':str(target)}


if __name__=='__main__':print(json.dumps(prepare(Path(__file__).resolve().parents[1])))
