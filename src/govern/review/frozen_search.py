"""Literal/path search over frozen text only; never consult the live worktree."""
import fnmatch
import json
import shutil
import subprocess
import tempfile
from .repository import safe_path


def search_frozen(snapshot,args):
    side=args.get('side','head')
    if side not in {'base','head','both'}:raise ValueError('invalid search side')
    query=args.get('query','');glob=args.get('path_glob','')
    if not isinstance(query,str) or len(query)>200 or not isinstance(glob,str) or len(glob)>200 or not (query or glob):raise ValueError('invalid query/path_glob')
    sides=['base','head'] if side=='both' else [side]
    hits=[];searched=0;engines=set();limit=40
    for version in sides:
        files={p:t for p,t in snapshot[version+'_files'].items() if not glob or fnmatch.fnmatchcase(p,glob)}
        searched+=len(files)
        if not query:
            engines.add('frozen-path-index')
            hits.extend({'path':p,'side':version,'line':1,'kind':'file','text':''} for p in sorted(files))
            continue
        if shutil.which('rg'):
            engines.add('ripgrep')
            with tempfile.TemporaryDirectory(prefix='govern-rg-') as folder:
                for path,text in files.items():
                    target=safe_path(folder,path);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text,encoding='utf-8',newline='')
                result=subprocess.run(['rg','--json','--hidden','--no-ignore','--fixed-strings','--max-count','41','--',query,'.'],cwd=folder,capture_output=True,text=True,encoding='utf-8',timeout=10)
                if result.returncode not in {0,1}:raise ValueError('frozen ripgrep search failed')
                for raw in result.stdout.splitlines():
                    item=json.loads(raw)
                    if item['type']!='match':continue
                    value=item['data'];path=value['path']['text'].replace('\\','/')
                    if path.startswith('./'):path=path[2:]
                    if path not in files:raise ValueError('search returned a path outside frozen inputs')
                    hits.append({'path':path,'side':version,'line':value['line_number'],'text':value['lines']['text'][:800]})
        else:
            engines.add('python-fallback')
            hits.extend({'path':p,'side':version,'line':i+1,'text':line[:800]} for p,text in files.items() for i,line in enumerate(text.splitlines()) if query in line)
    hits.sort(key=lambda h:(sides.index(h['side']),h['path'],h['line']))
    return {'engine':'+'.join(sorted(engines)),'query':query,'path_glob':glob,'searched_sides':sides,'searched_files':searched,
            'hits':hits[:limit],'truncated':len(hits)>limit,
            'scope':'frozen-text-only','limitation':'An empty result only describes this query and scope, not repository-wide absence.'}
