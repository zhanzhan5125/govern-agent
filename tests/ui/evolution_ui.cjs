// Real headless browser + actual web assets; API responses are controlled fixtures.
// Run with Playwright available on NODE_PATH. Does not touch business data or LLMs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');

async function main() {
  const web = path.resolve(__dirname, '../../web');
  const server = http.createServer((req, res) => {
    const files = {'/': 'index.html', '/assets/app.js': 'app.js', '/assets/app.css': 'app.css', '/assets/login.css': 'login.css'};
    const file = files[new URL(req.url, 'http://localhost').pathname];
    if (!file) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(web, file)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, ...(process.platform === 'win32' ? {channel: 'msedge'} : {})});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const posts = [];
    const statuses = ['STATIC_PASSED', 'HOLDOUT_PASSED', 'HUMAN_APPROVED', 'SHADOW', 'SUPERSEDED'];
    const candidates = statuses.map((status, i) => ({
      id: `00000000-0000-0000-0000-${String(i).padStart(12, '0')}`,
      kind: 'prompt', name: i ? 'llm-review' : '<img src=x onerror="window.injected=true">',
      status, revision: i + 2, lineage: {change_diff: '+test'}, reports: {},
    }));
    let unauthorized = false;
    await page.route('**/*', async route => {
      const req = route.request();
      const url = new URL(req.url());
      if (!/^\/(api|v1)\//.test(url.pathname)) return route.continue();
      if (unauthorized) return route.fulfill({status: 401, json: {error: 'login required'}});
      if (req.method() === 'POST') {
        posts.push({path: url.pathname, body: req.postDataJSON()});
        return route.fulfill({json: {ok: true, candidates: [], frontier: []}});
      }
      let value = {};
      if (url.pathname === '/v1/evolution/candidates') value = {candidates};
      else if (url.pathname.startsWith('/v1/evolution/candidates/')) value = {candidate: candidates[4], release_head: {generation: 9}, history: []};
      else if (url.pathname === '/v1/evolution/status') value = {production_ready: false, model_configured: false};
      else if (url.pathname === '/api/failures') value = {cases: []};
      else if (url.pathname === '/api/dashboard') value = {counts: {}, tasks: [], agents: [], skills: [], latest_tasks: []};
      return route.fulfill({json: value});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#evolution`);
    await page.locator('#candidate-list article').nth(4).waitFor();
    assert.equal(await page.locator('#candidate-list img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);

    const form = page.locator('#evolution-form');
    await form.locator('[name=kind]').selectOption('skill');
    await form.locator('[name=skill_name]').fill('auth-review');
    await page.locator('#auto-evolve').click();
    await page.waitForFunction(() => document.querySelector('#auto-evolve').disabled === false);
    assert.equal(posts.at(-1).path, '/v1/skill-evolution/auto');
    assert.equal(posts.at(-1).body.skill_name, 'auth-review');
    await form.locator('[name=kind]').selectOption('prompt');
    await page.locator('#auto-evolve').click();
    await page.waitForFunction(() => document.querySelector('#auto-evolve').disabled === false);
    assert.equal(posts.at(-1).path, '/v1/evolution/auto');

    await form.locator('[name=prompt]').fill('Review diff JSON severity fix test.');
    await form.locator('button[type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('#evolution-form button').disabled);
    assert.equal(posts.at(-1).body.kind, 'prompt');
    assert.equal(posts.at(-1).body.name, 'auth-review');

    let count = posts.length;
    await page.locator('#compare-candidates').click();
    assert.equal(posts.length, count);
    const checks = page.locator('[data-compare-candidate]');
    await checks.nth(0).check();
    await checks.nth(1).check();
    await page.locator('#compare-candidates').click();
    await page.waitForFunction(() => !document.querySelector('#compare-candidates').disabled);
    assert.deepEqual(posts.at(-1), {path: '/v1/evolution/compare', body: {candidate_ids: candidates.slice(0, 2).map(x => x.id)}});
    await checks.nth(2).check();
    await checks.nth(3).check();
    count = posts.length;
    await page.locator('#compare-candidates').click();
    assert.equal(posts.length, count);

    await page.locator('[data-action=approve]').click();
    assert.equal(posts.length, count); // Missing reason never submits approval.
    await page.locator('#candidate-reason').fill('controlled UI test');
    for (const action of ['evaluate', 'approve', 'shadow', 'activate', 'rollback']) {
      await page.locator(`[data-action=${action}]`).click();
      await page.waitForFunction(a => !document.querySelector(`[data-action=${a}]`).disabled, action);
      const request = posts.at(-1);
      assert.ok(request.path.endsWith('/' + action));
      const index = ['evaluate', 'approve', 'shadow', 'activate', 'rollback'].indexOf(action);
      assert.equal(request.body.expected_revision, candidates[index].revision);
      if (action === 'rollback') assert.equal(request.body.expected_generation, 9);
    }
    await page.locator('[data-action=history]').first().click();
    await page.waitForFunction(() => document.querySelector('#evolution-result').textContent.includes('release_head'));
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => window.scrollTo(0, 0));
    if (process.env.EVOAGENT_UI_SCREENSHOT) {
      const output = path.resolve(process.env.EVOAGENT_UI_SCREENSHOT);
      fs.mkdirSync(path.dirname(output), {recursive: true});
      await page.screenshot({path: output, fullPage: true});
    }
    unauthorized = true;
    await page.locator('#refresh').click();
    await page.locator('#login-overlay:not(.hidden)').waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: Prompt/Skill routing, candidate creation/comparison, stage payloads, reason guard, rollback generation, audit, escaped HTML, 401 login.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
