// Reproducibly prepare the pinned upstream source without resetting an existing checkout.
import { readFileSync, existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, dirname, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
const root=dirname(fileURLToPath(import.meta.url));
const upstream=JSON.parse(readFileSync(join(root,'UPSTREAM.json'),'utf8'));
const vendor=join(root,'vendor','pi'),patch=resolve(root,upstream.patch);
function git(args,check=true){
  const p=spawnSync('git',args,{cwd:vendor,encoding:'utf8'});
  if(check&&(p.error||p.status!==0))throw Error(p.error?.message||p.stderr);
  return p;
}
if(!existsSync(join(vendor,'.git'))){
  if(existsSync(vendor))throw Error('Vendor directory already exists without Git; inspect it manually.');
  mkdirSync(vendor,{recursive:true});git(['init']);git(['remote','add','origin',upstream.repository]);
  git(['sparse-checkout','init','--cone']);git(['sparse-checkout','set','packages/agent','packages/ai']);
  git(['fetch','--depth=1','origin',upstream.commit]);git(['checkout','--detach','FETCH_HEAD']);
}
if(git(['rev-parse','HEAD']).stdout.trim()!==upstream.commit)throw Error('Unexpected vendor revision; refusing to overwrite it.');
if(git(['apply','--reverse','--check',patch],false).status===0){console.log('Pinned Govern Core patch already applied.');}
else{
  if(git(['status','--porcelain']).stdout.trim())throw Error('Vendor has local changes; refusing to overwrite them.');
  git(['apply','--check',patch]);git(['apply',patch]);console.log('Pinned Govern Core patch applied.');
}
console.log('Next: pnpm install --frozen-lockfile; pnpm build:core; pnpm check; pnpm test');
