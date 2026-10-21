"""Start the real local product. Optionally import an explicitly selected legacy .env."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--model-env',help='Explicit existing project .env to import; values are never printed')
    parser.add_argument('--verification-image',default='')
    args=parser.parse_args()
    os.chdir(ROOT)
    config_path=ROOT/'.govern/config.json';config_path.parent.mkdir(exist_ok=True)
    config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    if args.model_env and not config.get('model'):
        values={}
        for line in Path(args.model_env).read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k,v=line.split('=',1);values[k.strip()]=v.strip().strip('"\'')
        base=values.get('EVOAGENT_LLM_BASE_URL') or values.get('API','')
        model=values.get('EVOAGENT_LLM_MODEL') or values.get('MODEL','')
        key=values.get('EVOAGENT_LLM_API_KEY') or values.get('APIKEY','')
        if all((base,model,key)):config['model']={'provider':'custom','base_url':base.rstrip('/').removesuffix('/chat/completions'),'model':model,'api_key':key,'headers':{}}
    if args.verification_image:
        config.setdefault('execution_profiles',{})['python-pytest']={'image':args.verification_image,'runner':'pytest','timeout':120}
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    os.environ['GOVERN_CONFIG']=str(config_path)
    os.environ['EVOAGENT_DB_PATH']=str(ROOT/'.govern/govern.sqlite')
    os.environ['EVOAGENT_PORT']=str(args.port)
    os.environ['EVOAGENT_HOST']='127.0.0.1'
    if config.get('model'):
        model=config['model']
        os.environ.update(EVOAGENT_LLM_PROVIDER='custom',EVOAGENT_LLM_BASE_URL=model['base_url'],EVOAGENT_LLM_MODEL=model['model'],EVOAGENT_LLM_API_KEY=model['api_key'])
    node=os.environ.get('GOVERN_NODE') or shutil.which('node')
    def supported(candidate):
        if not candidate:return False
        try:return int(subprocess.check_output([candidate,'--version'],text=True).strip().lstrip('v').split('.')[0])>=22
        except (OSError,ValueError,subprocess.SubprocessError):return False
    if not supported(node):
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if bundled.exists() and supported(str(bundled)):node=str(bundled)
        else:raise SystemExit('Pi运行层需要Node 22.19+，请设置GOVERN_NODE指向新版Node；当前服务未启动。')
    if node:os.environ['GOVERN_NODE']=node
    from govern.api.api import run
    run()


if __name__=='__main__':main()
