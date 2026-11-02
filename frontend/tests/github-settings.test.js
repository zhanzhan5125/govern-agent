import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
test('GitHub credentials use a password input and are never persisted to browser storage',()=>{
 const s=readFileSync(new URL('../src/App.vue',import.meta.url),'utf8');
 assert.match(s,/id="github-token"[^>]*type="password"/);
 assert.match(s,/githubForm.token=''/);
 assert.match(s,/test-github/);
 assert.ok(!/setItem\([^\n]*githubForm/.test(s));
});
