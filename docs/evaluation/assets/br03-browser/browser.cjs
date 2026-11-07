const puppeteer = require('/browser-deps/puppeteer');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const out = '/evidence';
const pause = ms => new Promise(r => setTimeout(r, ms));
const mark = (step, detail='') => console.log(new Date().toISOString(),step,detail);
const safeUrl = value => {try {const u=new URL(value);return u.origin+u.pathname;}catch{return '(invalid URL)';}};
(async () => {
  mark('LAUNCH_START');
  const browser = await puppeteer.launch({headless:true,
    timeout:30000,protocolTimeout:30000,
    executablePath:'/ms-playwright/chromium-1248/chrome-linux-arm64/chrome',
    args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
  mark('LAUNCH_DONE');
  mark('NEW_PAGE_START');
  const page = await browser.newPage();
  mark('NEW_PAGE_DONE');
  page.setDefaultTimeout(30000);page.setDefaultNavigationTimeout(30000);
  page.on('requestfailed',request=>mark('REQUEST_FAILED',JSON.stringify({
    url:safeUrl(request.url()),error:request.failure()?.errorText})));
  mark('VIEWPORT_START');
  await page.setViewport({width:1600,height:1200});
  mark('VIEWPORT_DONE');
  const steps = [];
  async function snapshot(name) {
    const state = await page.evaluate(() => ({text:document.body.innerText,
      metrics:[...document.querySelectorAll('[data-testid="stMetric"]')].map(x=>x.innerText.replace(/\n+/g,'\n')),
      selections:[...document.querySelectorAll('[data-testid="stSelectbox"] input')].map(x=>x.value),
      sliders:[...document.querySelectorAll('input[type="range"], [role="slider"]')].map(x=>({
        value:x.getAttribute('aria-valuenow') ?? x.value,max:x.getAttribute('aria-valuemax') ?? x.max,
        text:x.getAttribute('aria-valuetext')})),
      exceptions:[...document.querySelectorAll('[data-testid="stException"]')].map(x=>x.innerText),
      charts:document.querySelectorAll('[data-testid="stPlotlyChart"]').length}));
    assert.deepEqual(state.exceptions,[]);
    steps.push({name,...state});
    console.log('STATE',name,JSON.stringify(state.metrics));
    fs.writeFileSync(out+'/session.json',JSON.stringify({steps},null,2)+'\n');
    return state;
  }
  async function wait(text, limit=180) {
    mark('WAIT_START',text);
    const deadline=Date.now()+limit*500;
    while(Date.now()<deadline) {
      if(await page.evaluate(t=>document.body.innerText.includes(t)
        || [...document.querySelectorAll('input')].some(x=>x.value.includes(t))
        || [...document.querySelectorAll('[aria-valuetext]')].some(x=>x.getAttribute('aria-valuetext').includes(t)),text)) {
        await pause(1800);mark('WAIT_DONE',text); return;
      }
      await pause(500);
    }
    throw Error('Visible text missing: '+text+'\n'+await page.evaluate(()=>document.body.innerText));
  }
  async function radio(text) {
    mark('RADIO_START',text);
    await page.evaluate(t=>{const x=[...document.querySelectorAll('[data-testid="stRadio"] label')]
      .find(x=>x.innerText.trim()===t || x.innerText.startsWith(t));
      if(!x)throw Error('Missing radio '+t);x.click();},text);
    await pause(2200);
    mark('RADIO_DONE',text);
  }
  async function select(label, text) {
    mark('SELECT_START',label+' '+text);
    const id = await page.evaluate(l=>{const root=[...document.querySelectorAll('[data-testid="stSelectbox"]')]
      .find(x=>x.innerText.includes(l));if(!root)throw Error('Missing select '+l);
      const input=root.querySelector('input');input.focus();input.click();return input.id;},label);
    await page.keyboard.type(text);await page.keyboard.press('Enter');await pause(3000);
    mark('SELECT_DONE',label+' '+text);return id;
  }
  async function click(text) {
    mark('CLICK_START',text);
    await page.evaluate(t=>{const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()===t);
      if(!b)throw Error('Missing button '+t);b.click()},text);await pause(2500);mark('CLICK_DONE',text);
  }
  async function input(label, value) {
    mark('INPUT_START',label);
    const target = await page.evaluate(l=>{const root=[...document.querySelectorAll('[data-testid="stTextInput"], [data-testid="stNumberInput"], [data-testid="stDateInput"], [data-testid="stTimeInput"]')]
      .find(x=>x.innerText.includes(l));if(!root)throw Error('Missing input '+l);
      const el=root.querySelector('input');return {type:el.type,value:el.value,isTime:root.dataset.testid==='stTimeInput',inForm:!!root.closest('[data-testid="stForm"]')};},label);
    mark('INPUT_CONTROL',JSON.stringify(target));
    if(target.type==='date') {
      assert.equal(value,'2019/12/30');
      await page.evaluate(l=>[...document.querySelectorAll('[data-testid="stDateInput"]')]
        .find(x=>x.innerText.includes(l)).querySelector('[data-testid="stDateInputCalendarButton"]').click(),label);
      await pause(500);
      await page.evaluate(()=>{const day=document.querySelector('[role="button"][aria-label="Monday, December 30, 2019"]');
        if(!day)throw Error('December30,2019 calendar day absent');day.click();});
      await wait('Nguồn [2019-12-30T00:00:00');
    } else if(target.isTime) {
      for(const [segment,part] of [['hour','13'],['minute','55']]) {
        await page.evaluate(s=>document.querySelector('[data-testid="stTimeInput"] [role="spinbutton"][data-type="'+s+'"]').focus(),segment);
        await page.keyboard.type(part);
      }
      const segments=await page.evaluate(()=>[...document.querySelectorAll('[data-testid="stTimeInput"] [role="spinbutton"]')]
        .map(x=>x.getAttribute('aria-valuenow')));
      assert.deepEqual(segments,['13','55']);await page.keyboard.press('Tab');
      await wait('Nguồn [2019-12-30T13:55:00');
    } else {
      await page.evaluate(l=>{const root=[...document.querySelectorAll('[data-testid="stTextInput"], [data-testid="stNumberInput"]')]
        .find(x=>x.innerText.includes(l));const el=root.querySelector('input:not([hidden])');el.focus();el.select();},label);
      await page.keyboard.down('Control');await page.keyboard.press('A');await page.keyboard.up('Control');
      await page.keyboard.type(value);
    }
    if(target.type!=='date' && !target.isTime) {
      if(!target.inForm)await page.keyboard.press('Enter');
      await page.keyboard.press('Tab');
    }
    await pause(2500);
    const observed=await page.evaluate(l=>{
      const root=[...document.querySelectorAll('[data-testid="stTextInput"], [data-testid="stNumberInput"], [data-testid="stDateInput"], [data-testid="stTimeInput"]')]
        .find(x=>x.innerText.includes(l));return root.querySelector('input').value;},label);
    mark('INPUT_DONE',label+' '+observed);
    if(target.type==='date')assert.equal(observed,value.replaceAll('/','-'));
    if(target.isTime)assert(observed==='13:55' || observed==='13:55:00');
  }
  async function download(label) {
    const before=new Set(fs.readdirSync(out));
    await page.evaluate(t=>{const b=[...document.querySelectorAll('[data-testid="stDownloadButton"] button')]
      .find(x=>x.innerText===t);if(!b)throw Error('Missing download '+t);b.click()},label);
    const deadline=Date.now()+90000;
    while(Date.now()<deadline) {
      const files=fs.readdirSync(out).filter(x=>!before.has(x)&&!x.endsWith('.crdownload'));
      if(files.length){const file=files[0];console.log('DOWNLOAD',file);return file;}
      await pause(500);
    }
    throw Error('Download timed out '+label);
  }
  try {
    fs.mkdirSync(out,{recursive:true});
    mark('DOWNLOAD_SESSION_START');
    const cdp=await page.createCDPSession();
    await cdp.send('Page.setDownloadBehavior',{behavior:'allow',downloadPath:out});
    mark('DOWNLOAD_SESSION_DONE');
    const url=process.env.APP_URL || 'http://app:8501';mark('GOTO_START',safeUrl(url));
    await page.goto(url,{waitUntil:'networkidle2',timeout:30000});
    mark('GOTO_DONE');mark('RADIO_SELECTOR_START');
    await page.waitForSelector('[data-testid="stRadio"]');
    mark('RADIO_SELECTOR_DONE');
    await radio('🧪 EC/pH');await wait('EC/pH dung dịch drain');
    await select('Khoang · vụ drain','Reference');await wait('agc-cycle-Reference');
    await wait('rule_not_configured');
    const noRule=await snapshot('Reference no-rule raw browsing');
    assert.equal(noRule.selections[1],'Reference — agc-cycle-Reference');
    assert(noRule.metrics.includes('EC ngoài dải (phút)\nNULL'));
    await select('Profile EC/pH','drain-demo-b03:Reference:v1');
    await wait('80400 phút gốc');await wait('2019-12-31T12:35:00');
    await wait('2020-02-25T08:35:00');
    const full=await snapshot('Reference demo global longest EC');
    assert(full.text.includes('144 mốc / 16080'));
    await click('Trace sau ▶');await wait('offset 144');
    await snapshot('Long event bounded trace second page');
    await click('◀ Trace trước');await wait('offset 0');
    await input('Ngày nguồn','2019/12/30');
    await wait('Nguồn [2019-12-30T00:00:00');
    await input('Giờ nguồn','13:55');
    await wait('2019-12-30T13:55:00');
    await click('Xác minh lineage về raw');
    await wait('Raw hash xác minh khớp');await wait('dòng 4201');
    const suspect=await snapshot('Reference pH-zero own quality and verified raw line 4201');
    assert(suspect.text.includes('Reference/GreenhouseClimate.csv'));
    const files=[];
    for(const label of ['CSV summary drain','CSV joint OR drain','CSV đợt drain (đã lọc)',
      'JSON result drain (typed)','CSV nước theo ngày','CSV nguồn drain (trang)','CSV detail drain (trang)']) {
      files.push(await download(label));
    }
    await radio('ph');await pause(3000);
    const filtered=await snapshot('pH-only list preserves summary totals');
    assert.deepEqual(filtered.metrics.slice(0,11),full.metrics.slice(0,11));
    await page.evaluate(()=>{
      const slider=[...document.querySelectorAll('[data-testid="stSlider"]')]
        .find(x=>x.innerText.includes('Thời lượng tối thiểu')).querySelector('input[type="range"], [role="slider"]');
      slider.focus();
    });
    await page.keyboard.press('End');await pause(3000);
    const minimum=await snapshot('Minimum-duration list filter preserves summary totals');
    assert.deepEqual(minimum.metrics.slice(0,11),full.metrics.slice(0,11));
    assert.equal(minimum.sliders[0].value,minimum.sliders[0].max);
    assert.notEqual(minimum.sliders[0].value,filtered.sliders[0].value);
    const minimumText=await page.evaluate(()=>[...document.querySelectorAll('[data-testid="stSlider"]')]
      .find(x=>x.innerText.includes('Thời lượng tối thiểu')).innerText);
    assert(minimumText.includes('1440'));
    await select('Profile EC/pH','(không chọn');await wait('KPI ngoài dải là NULL');
    await snapshot('Cleared profile removes event detail, raw browsing remains');
    await radio('Cả hai');
    await page.evaluate(()=>{const x=[...document.querySelectorAll('[data-testid="stExpander"] summary')]
      .find(x=>x.innerText.includes('Tạo version profile'));x.click()});
    await input('Tên profile','browser_br03');
    await input('Nguồn / lý do','Browser acceptance experiment; unverified bounds, no agricultural claim');
    await click('Lưu version bất biến');await wait('browser_br03:v1');
    const v1=await snapshot('Engineer profile v1 saved');
    assert.deepEqual(v1.metrics.slice(0,11),full.metrics.slice(0,11));
    await input('Version mới','2');await input('EC cận trên','10');
    await click('Lưu version bất biến');await wait('browser_br03:v2');
    const v2=await snapshot('Engineer profile v2 changes EC out total');
    assert.notEqual(v2.metrics[0],v1.metrics[0]);
    await select('Profile EC/pH','browser_br03:v1');await pause(3000);
    const replay=await snapshot('Engineer profile v1 reopened exactly');
    assert.deepEqual(replay.metrics.slice(0,11),v1.metrics.slice(0,11));
    await select('Profile EC/pH','drain-demo-b03:Reference:v1');await wait('80400 phút gốc');
    const hashes=Object.fromEntries(files.map(file=>[file,{bytes:fs.statSync(out+'/'+file).size,
      sha256:crypto.createHash('sha256').update(fs.readFileSync(out+'/'+file)).digest('hex')}]));
    fs.writeFileSync(out+'/session.json',JSON.stringify({generated_at:new Date().toISOString(),
      runtime:'Docker Chromium + Puppeteer, 0.5CPU/768MiB',node:process.version,
      browser:await browser.version(),url:safeUrl(url),steps,downloads:hashes},null,2)+'\n');
    console.log('BROWSER_PASS',JSON.stringify(hashes));
  } finally {mark('CLOSE_START');await Promise.race([browser.close(),pause(15000).then(()=>{throw Error('Browser close deadline exceeded');})]);mark('CLOSE_DONE');}
})().catch(e=>{console.error(e);process.exit(1)});
