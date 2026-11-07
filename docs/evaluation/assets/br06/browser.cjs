let puppeteer;
try {
  puppeteer = require('/deps/node_modules/puppeteer-core');
} catch {
  try {
    puppeteer = require('puppeteer-core');
  } catch {
    try {
      puppeteer = require('/tmp/node_modules/puppeteer-core');
    } catch {
      puppeteer = require('puppeteer');
    }
  }
}
const fs = require('node:fs');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const out = '/evidence';
const pause = ms => new Promise(r => setTimeout(r, ms));
const mark = (step, detail = '') => console.log(new Date().toISOString(), step, detail);
const safeUrl = value => {
  try {
    const u = new URL(value);
    return u.origin + u.pathname;
  } catch {
    return '(invalid URL)';
  }
};

const chromePath = fs.existsSync('/ms-playwright/chromium-1248/chrome-linux-arm64/chrome')
  ? '/ms-playwright/chromium-1248/chrome-linux-arm64/chrome'
  : (fs.existsSync('/ms-playwright/chromium-1248/chrome-linux/chrome')
      ? '/ms-playwright/chromium-1248/chrome-linux/chrome'
      : undefined);

(async () => {
  mark('LAUNCH_START');
  const browser = await puppeteer.launch({
    headless: true,
    timeout: 30000,
    protocolTimeout: 30000,
    executablePath: chromePath,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-features=AutoupgradeMixedContent,HttpsFirstMode,HttpsUpgrades']
  });
  mark('LAUNCH_DONE');

  mark('NEW_PAGE_START');
  const page = await browser.newPage();
  mark('NEW_PAGE_DONE');
  page.setDefaultTimeout(30000);
  page.setDefaultNavigationTimeout(30000);

  page.on('requestfailed', r => mark('REQUEST_FAILED', JSON.stringify({ url: safeUrl(r.url()), error: r.failure()?.errorText })));
  await page.setViewport({ width: 1600, height: 1200 });
  const steps = [];

  async function wait(text) {
    mark('WAIT_START', text);
    const end = Date.now() + 90000;
    while (Date.now() < end) {
      if (await page.evaluate(t => document.body.innerText.includes(t) || [...document.querySelectorAll('input')].some(i => i.value.includes(t)), text)) {
        await pause(1500);
        mark('WAIT_DONE', text);
        return;
      }
      await pause(500);
    }
    throw Error('Expected ' + text + '\n' + await page.evaluate(() => document.body.innerText));
  }

  async function snapshot(name) {
    const state = await page.evaluate(() => ({
      text: document.body.innerText,
      metrics: [...document.querySelectorAll('[data-testid="stMetric"]')].map(e => e.innerText),
      selections: [...document.querySelectorAll('[data-testid="stSelectbox"] input')].map(e => e.value),
      exceptions: [...document.querySelectorAll('[data-testid="stException"]')].map(e => e.innerText),
    }));
    assert.deepEqual(state.exceptions, []);
    steps.push({ name, ...state });
    fs.writeFileSync(out + '/session.json', JSON.stringify({ steps }, null, 2) + '\n');
    mark('STATE', name);
    return state;
  }

  async function select(label, text, p = page) {
    mark('SELECT_START', label + ' ' + text);
    await p.evaluate(l => {
      const root = [...document.querySelectorAll('[data-testid="stSelectbox"]')].find(x => x.innerText.includes(l));
      if (!root) throw Error('Missing select ' + l);
      const i = root.querySelector('input');
      i.focus();
      i.click();
    }, label);
    await p.keyboard.down('Control');
    await p.keyboard.press('KeyA');
    await p.keyboard.up('Control');
    await p.keyboard.down('Meta');
    await p.keyboard.press('KeyA');
    await p.keyboard.up('Meta');
    await p.keyboard.press('Backspace');
    await pause(200);
    await p.keyboard.type(text);
    await pause(500);
    await p.keyboard.press('Enter');
    await pause(3000);
    mark('SELECT_DONE', text);
  }

  async function click(label) {
    mark('CLICK_START', label);
    await page.evaluate(l => {
      const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim().includes(l));
      if (!b) throw Error('Missing button ' + l);
      b.click();
    }, label);
    await pause(2500);
    mark('CLICK_DONE', label);
  }

  async function download(label) {
    const before = new Set(fs.readdirSync(out));
    await click(label);
    const end = Date.now() + 60000;
    while (Date.now() < end) {
      const f = fs.readdirSync(out).find(x => !before.has(x) && !x.endsWith('.crdownload'));
      if (f) {
        mark('DOWNLOAD', f);
        return f;
      }
      await pause(500);
    }
    throw Error('Download timeout ' + label);
  }

  async function typeInput(labelText, textToType) {
    mark('TYPE_START', labelText + ' -> ' + textToType);
    const handle = await page.evaluateHandle(lbl => {
      const inputs = [...document.querySelectorAll('input')];
      const byAria = inputs.find(i => i.getAttribute('aria-label')?.includes(lbl));
      if (byAria) return byAria;
      const byContainer = [...document.querySelectorAll('[data-testid="stTextInput"]')].find(c => c.innerText.includes(lbl))?.querySelector('input');
      if (byContainer) return byContainer;
      return inputs.find(i => i.value === '20.0' || i.value === '-150.0');
    }, labelText);
    assert(handle, 'Could not find input for label ' + labelText);

    await handle.focus();
    await handle.click();
    await pause(200);

    // Puppeteer focus/click + keyboard Control+A (and Meta+A) + Backspace
    await page.keyboard.down('Control');
    await page.keyboard.press('KeyA');
    await page.keyboard.up('Control');
    await page.keyboard.down('Meta');
    await page.keyboard.press('KeyA');
    await page.keyboard.up('Meta');
    await page.keyboard.press('Backspace');
    await pause(100);

    // Type exact text
    await page.keyboard.type(textToType, { delay: 20 });
    // Tab/blur to commit change to React-controlled Streamlit state
    await page.keyboard.press('Tab');
    await pause(500);

    // Assert actual displayed input value before proceeding
    const actualVal = await page.evaluate(el => el.value, handle);
    assert.equal(actualVal, textToType, `Expected displayed input value "${textToType}", got "${actualVal}"`);
    mark('TYPE_DONE', labelText + ' = ' + actualVal);
  }

  async function getScenarioCount() {
    return await page.evaluate(() => {
      const m = document.body.innerText.match(/Kịch bản đã lưu \((\d+)\)/);
      if (m) return parseInt(m[1], 10);
      const sel = [...document.querySelectorAll('[data-testid="stSidebar"] [data-testid="stSelectbox"]')].find(x => x.innerText.includes('Chọn kịch bản giá'));
      return sel ? 0 : 0;
    });
  }

  function metric(state, label) {
    const entry = state.metrics.find(x => x.split('\n')[0] === label);
    assert(entry, 'Missing metric ' + label + '; available: ' + JSON.stringify(state.metrics));
    return entry.split('\n').slice(1).join('\n').trim();
  }

  const expectedAicu = JSON.parse(fs.readFileSync('/reference/actual-cost-AICU.json', 'utf8')).data;

  try {
    fs.mkdirSync(out, { recursive: true });
    const cdp = await page.createCDPSession();
    await cdp.send('Page.setDownloadBehavior', { behavior: 'allow', downloadPath: out });

    const url = process.env.APP_URL;
    mark('GOTO_START', safeUrl(url));
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
    mark('GOTO_DONE');

    await page.waitForSelector('[data-testid="stRadio"]');
    mark('RADIO_START');
    await page.evaluate(() => {
      const r = [...document.querySelectorAll('[data-testid="stRadio"] label')].find(x => x.innerText.includes('Chi phí & Kịch bản (BR-06)'));
      if (!r) throw Error('Cost radio absent');
      r.click();
    });

    await wait('Chi phí năng lượng & Kịch bản biểu giá');
    await wait('AICU');

    // 1. Primary Baseline View
    const baselineState = await snapshot('AICU baseline reference evaluation');
    assert(baselineState.text.includes('AGC_SHARED_BASIS_V1:v1'));
    assert(baselineState.text.includes('1.536'));
    assert.equal(metric(baselineState, 'Tổng chi phí (EUR)'), '1,154.65 EUR');
    assert.equal(metric(baselineState, 'Chi phí trên diện tích (EUR/m²)'), '18.47 EUR/m²');
    assert(metric(baselineState, 'Coverage định giá (%)').startsWith('100.0%'));
    assert(metric(baselineState, 'Coverage lượng (%)').startsWith('100.0%'));

    await page.screenshot({ path: out + '/aicu-cost-primary.png', fullPage: true });

    // 2. Tab Biểu giá: Rate table DOM verification
    await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('[role="tab"], button, div')].filter(t => t.innerText && t.innerText.includes('Biểu giá có hiệu lực'));
      const target = tabs.find(t => t.getAttribute('role') === 'tab' || t.tagName === 'BUTTON') || tabs[tabs.length - 1];
      if (!target) throw Error('Tariff tab not found');
      target.click();
    });
    await pause(2000);
    const tariffState = await snapshot('Baseline tariff rates table verification');
    assert(tariffState.text.includes('AGC 2020 Reference Tariff'), 'Expected AGC 2020 Reference Tariff');
    assert(tariffState.text.includes('Economics.pdf'), 'Expected Economics.pdf reference');
    const hasDf = await page.evaluate(() => document.querySelectorAll('[data-testid="stDataFrame"], [data-testid="stTable"], canvas').length > 0);
    assert(hasDf, 'Expected rate dataframe in DOM');

    // 3. Tab Tạo kịch bản: Input Validation Check (negative/invalid rejection without write)
    await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('[role="tab"], button, div')].filter(t => t.innerText && t.innerText.includes('Tạo / Sửa đổi kịch bản'));
      const target = tabs.find(t => t.getAttribute('role') === 'tab' || t.tagName === 'BUTTON') || tabs[tabs.length - 1];
      if (!target) throw Error('Manage tab not found');
      target.click();
    });
    await pause(2000);

    const initialScenarioCount = await getScenarioCount();
    mark('SCENARIO_COUNT_BEFORE_INVALID', String(initialScenarioCount));

    // Type invalid percent (-150%) into elec high pct field
    await typeInput('Phần trăm thay đổi điện cao điểm', '-150.0');
    await click('Lưu kịch bản bất biến');
    await pause(2000);

    const validationState = await snapshot('Validation error rejection for -150%');
    assert(validationState.text.includes('lớn hơn hoặc bằng -100'), 'Expected validation error for -150%; got text: ' + validationState.text);

    // Assert durable scenario count unchanged for invalid input
    const scenarioCountAfterInvalid = await getScenarioCount();
    mark('SCENARIO_COUNT_AFTER_INVALID', String(scenarioCountAfterInvalid));
    assert.equal(scenarioCountAfterInvalid, initialScenarioCount, 'Durable scenario count must remain unchanged after rejected invalid input');

    // Reset to valid percent (20.0%) and save scenario
    await typeInput('Phần trăm thay đổi điện cao điểm', '20.0');
    await click('Lưu kịch bản bất biến');
    await wait('So sánh kịch bản');

    const scenarioState = await snapshot('AICU electric +20% scenario comparison');
    assert(metric(scenarioState, 'Baseline (EUR)').startsWith('1,154.65 EUR'));
    assert(metric(scenarioState, 'Kịch bản (EUR)').startsWith('1,359.40 EUR'));
    assert(metric(scenarioState, 'Kịch bản (EUR)').includes('+204.75 EUR'));
    assert(metric(scenarioState, 'Kịch bản (EUR/m²)').startsWith('21.75 EUR/m²'));
    assert(metric(scenarioState, 'Kịch bản (EUR/m²)').includes('+3.28 EUR/m²'));

    // Capture exact created scenario ID for unique identity assertions across reopens
    const v1Option = scenarioState.selections.find(s => s.includes('scenario:req-') && s.endsWith(':v1)'));
    assert(v1Option, 'Expected created v1 scenario in selections; got: ' + JSON.stringify(scenarioState.selections));
    const targetScenarioId = v1Option.split('(')[1].split(':v1)')[0];
    const expectedV1Version = `${targetScenarioId}:v1`;
    const expectedV2Version = `${targetScenarioId}:v2`;
    mark('CREATED_SCENARIO_ID', targetScenarioId);

    await page.screenshot({ path: out + '/scenario-comparison.png', fullPage: true });

    // 4. Append Revision v2
    await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('[role="tab"], button, div')].filter(t => t.innerText && t.innerText.includes('Tạo / Sửa đổi kịch bản'));
      const target = tabs.find(t => t.getAttribute('role') === 'tab' || t.tagName === 'BUTTON') || tabs[tabs.length - 1];
      if (target) target.click();
    });
    await pause(2000);

    await page.evaluate(() => {
      const radios = [...document.querySelectorAll('[data-testid="stRadio"] label')];
      const revRadio = radios.find(r => r.innerText.includes('Thêm phiên bản sửa đổi (Revision)'));
      if (revRadio) revRadio.click();
    });
    await pause(1500);

    await click('Lưu kịch bản bất biến');
    await wait('So sánh kịch bản');
    const v2State = await snapshot('AICU v2 revision append comparison');

    // Assert durable identity, context, rates, and values on exact v2
    assert(metric(v2State, 'Baseline (EUR)').startsWith('1,154.65 EUR'));
    assert(metric(v2State, 'Kịch bản (EUR)').startsWith('1,359.40 EUR'));
    assert(metric(v2State, 'Kịch bản (EUR)').includes('+204.75 EUR'));
    assert(metric(v2State, 'Kịch bản (EUR/m²)').startsWith('21.75 EUR/m²'));
    assert(metric(v2State, 'Kịch bản (EUR/m²)').includes('+3.28 EUR/m²'));
    const v2SelectVal = v2State.selections.find(s => s.includes(expectedV2Version));
    assert(v2SelectVal, `Expected exact v2 version ${expectedV2Version} selected in sidebar; got: ` + JSON.stringify(v2State.selections));

    // 5. Old-v1 Reopen after v2: assert v1 was not overwritten by v2 using exact unique scenario version
    mark('OLD_V1_REOPEN_START');
    await select('Chọn kịch bản giá', expectedV1Version);
    await wait('So sánh kịch bản');
    const v1ReopenState = await snapshot('AICU old-v1 reopen after v2');
    assert(metric(v1ReopenState, 'Baseline (EUR)').startsWith('1,154.65 EUR'));
    assert(metric(v1ReopenState, 'Kịch bản (EUR)').startsWith('1,359.40 EUR'));
    assert(metric(v1ReopenState, 'Kịch bản (EUR)').includes('+204.75 EUR'));
    assert(metric(v1ReopenState, 'Kịch bản (EUR/m²)').startsWith('21.75 EUR/m²'));
    assert(metric(v1ReopenState, 'Kịch bản (EUR/m²)').includes('+3.28 EUR/m²'));
    const v1ReopenSelectVal = v1ReopenState.selections.find(s => s.includes(expectedV1Version));
    assert(v1ReopenSelectVal, `Expected exact v1 version ${expectedV1Version} in sidebar upon reopen; got: ` + JSON.stringify(v1ReopenState.selections));
    mark('OLD_V1_REOPEN_DONE');

    // Return to exact v2 for exports
    await select('Chọn kịch bản giá', expectedV2Version);
    await wait('So sánh kịch bản');

    // 6. Fresh Browser Session Reopen: open brand-new page with fresh session, load exact v2
    mark('FRESH_SESSION_REOPEN_START');
    const page2 = await browser.newPage();
    page2.setDefaultTimeout(30000);
    page2.setDefaultNavigationTimeout(30000);
    await page2.setViewport({ width: 1600, height: 1200 });
    await page2.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
    await page2.waitForSelector('[data-testid="stRadio"]');
    await page2.evaluate(() => {
      const r = [...document.querySelectorAll('[data-testid="stRadio"] label')].find(x => x.innerText.includes('Chi phí & Kịch bản (BR-06)'));
      if (!r) throw Error('Cost radio absent');
      r.click();
    });

    const waitPage2 = async (text) => {
      const end = Date.now() + 90000;
      while (Date.now() < end) {
        if (await page2.evaluate(t => document.body.innerText.includes(t) || [...document.querySelectorAll('input')].some(i => i.value.includes(t)), text)) {
          await pause(1500);
          return;
        }
        await pause(500);
      }
      throw Error('Expected on page2: ' + text);
    };

    await waitPage2('Chi phí năng lượng & Kịch bản biểu giá');
    await waitPage2('AICU');

    // Select exact v2 version on fresh session
    await select('Chọn kịch bản giá', expectedV2Version, page2);
    await waitPage2('So sánh kịch bản');

    const freshState = await page2.evaluate(() => ({
      text: document.body.innerText,
      metrics: [...document.querySelectorAll('[data-testid="stMetric"]')].map(e => e.innerText),
      selections: [...document.querySelectorAll('[data-testid="stSelectbox"] input')].map(e => e.value),
      exceptions: [...document.querySelectorAll('[data-testid="stException"]')].map(e => e.innerText),
    }));
    assert.deepEqual(freshState.exceptions, []);
    assert(metric(freshState, 'Baseline (EUR)').startsWith('1,154.65 EUR'));
    assert(metric(freshState, 'Kịch bản (EUR)').startsWith('1,359.40 EUR'));
    assert(metric(freshState, 'Kịch bản (EUR)').includes('+204.75 EUR'));
    assert(metric(freshState, 'Kịch bản (EUR/m²)').startsWith('21.75 EUR/m²'));
    assert(metric(freshState, 'Kịch bản (EUR/m²)').includes('+3.28 EUR/m²'));
    const freshSelectVal = freshState.selections.find(s => s.includes(expectedV2Version));
    assert(freshSelectVal, `Expected exact ${expectedV2Version} selected in fresh session; got: ` + JSON.stringify(freshState.selections));
    steps.push({ name: 'AICU fresh session reopen v2 durable verification', ...freshState });
    await page2.close();
    mark('FRESH_SESSION_REOPEN_DONE');

    // 7. Select Export Tab and Download 7 Files
    await page.evaluate(() => {
      const all = [...document.querySelectorAll('[role="tab"], button, div')].filter(t => t.innerText && t.innerText.includes('Xuất dữ liệu'));
      const exportTab = all.find(t => t.getAttribute('role') === 'tab' || t.tagName === 'BUTTON') || all[all.length - 1];
      if (!exportTab) throw Error('Export tab absent');
      exportTab.click();
    });
    await pause(2000);

    const files = [];
    for (const label of [
      'CSV Summary chi phí (Baseline vs Kịch bản)',
      'CSV Chi phí thành phần (Component Costs)',
      'CSV Chi tiết từng ngày (Daily Costs & Rates)',
      'CSV Chi tiết từng tuần (Weekly Costs & Rates)',
      'CSV Biểu giá hiệu lực (Tariff & Scenario Rates)',
      'JSON Chi phí (ScenarioComparison typed)',
      'JSON Kịch bản DTO (CostScenario typed)'
    ]) {
      files.push(await download(label));
    }

    const hashes = Object.fromEntries(
      files.map(f => [
        f,
        {
          bytes: fs.statSync(out + '/' + f).size,
          sha256: crypto.createHash('sha256').update(fs.readFileSync(out + '/' + f)).digest('hex')
        }
      ])
    );

    fs.writeFileSync(
      out + '/session.json',
      JSON.stringify(
        {
          generated_at: new Date().toISOString(),
          runtime: 'Docker Chromium/Puppeteer 0.5CPU/768MiB',
          node: process.version,
          browser: await browser.version(),
          url: safeUrl(url),
          publication: expectedAicu.context.publication_id,
          steps,
          downloads: hashes
        },
        null,
        2
      ) + '\n'
    );
    mark('BROWSER_PASS', JSON.stringify(hashes));
  } finally {
    mark('CLOSE_START');
    await Promise.race([browser.close(), pause(15000).then(() => { throw Error('Close timeout'); })]);
    mark('CLOSE_DONE');
  }
})().catch(e => {
  console.error(e);
  process.exit(1);
});
