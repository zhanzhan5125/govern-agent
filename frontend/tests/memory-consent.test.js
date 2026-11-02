import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('historical memory has a separate unchecked per-run disclosure consent',()=>{
 const source=readFileSync(new URL('../src/App.vue',import.meta.url),'utf8');
 assert.match(source,/const memoryConsent=ref\(false\)/);
 assert.match(source,/startReview\(\).*memoryConsent.value=false/);
 assert.match(source,/allow_external_memory:memoryConsent.value/);
 assert.match(source,/v-model="memoryConsent"/);
 assert.match(source,/仅本次有效/);
});
