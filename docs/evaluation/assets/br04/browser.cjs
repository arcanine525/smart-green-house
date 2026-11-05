const puppeteer=require('/browser-deps/puppeteer');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const out='/evidence';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const mark=(step,detail='')=>console.log(new Date().toISOString(),step,detail);
const safeUrl=value=>{try{const u=new URL(value);return u.origin+u.pathname;}catch{return '(invalid URL)';}};
(async()=>{
 mark('LAUNCH_START');
 const browser=await puppeteer.launch({headless:true,timeout:30000,protocolTimeout:30000,
  executablePath:'/ms-playwright/chromium-1248/chrome-linux-arm64/chrome',
  args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
 mark('LAUNCH_DONE');mark('NEW_PAGE_START');const page=await browser.newPage();mark('NEW_PAGE_DONE');
 page.setDefaultTimeout(30000);page.setDefaultNavigationTimeout(30000);
 page.on('requestfailed',r=>mark('REQUEST_FAILED',JSON.stringify({url:safeUrl(r.url()),error:r.failure()?.errorText})));
 await page.setViewport({width:1600,height:1200});const steps=[];
 async function wait(text){mark('WAIT_START',text);const end=Date.now()+90000;
  while(Date.now()<end){if(await page.evaluate(t=>document.body.innerText.includes(t)||[...document.querySelectorAll('input')].some(i=>i.value.includes(t)),text)){await pause(1500);mark('WAIT_DONE',text);return;}await pause(500);}
  throw Error('Expected '+text+'\n'+await page.evaluate(()=>document.body.innerText));}
 async function snapshot(name){const state=await page.evaluate(()=>({text:document.body.innerText,
   metrics:[...document.querySelectorAll('[data-testid="stMetric"]')].map(e=>e.innerText),
   selections:[...document.querySelectorAll('[data-testid="stSelectbox"] input')].map(e=>e.value),
   exceptions:[...document.querySelectorAll('[data-testid="stException"]')].map(e=>e.innerText),
   charts:document.querySelectorAll('[data-testid="stPlotlyChart"]').length}));assert.deepEqual(state.exceptions,[]);
   steps.push({name,...state});fs.writeFileSync(out+'/session.json',JSON.stringify({steps},null,2)+'\n');mark('STATE',name);return state;}
 async function select(label,text){mark('SELECT_START',label+' '+text);
  await page.evaluate(l=>{const root=[...document.querySelectorAll('[data-testid="stSelectbox"]')].find(x=>x.innerText.includes(l));if(!root)throw Error('Missing select '+l);const i=root.querySelector('input');i.focus();i.click();},label);
  await page.keyboard.type(text);await page.keyboard.press('Enter');await pause(3000);mark('SELECT_DONE',text);}
 async function click(label){mark('CLICK_START',label);await page.evaluate(l=>{const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()===l);if(!b)throw Error('Missing '+l);b.click();},label);await pause(2200);mark('CLICK_DONE',label);}
 async function download(label){const before=new Set(fs.readdirSync(out));await click(label);const end=Date.now()+60000;
  while(Date.now()<end){const f=fs.readdirSync(out).find(x=>!before.has(x)&&!x.endsWith('.crdownload'));if(f){mark('DOWNLOAD',f);return f;}await pause(500);}throw Error('Download timeout '+label);}
 function metric(state,label){const entry=state.metrics.find(x=>x.split('\n')[0]===label);assert(entry,'Missing metric '+label);return entry.split('\n').slice(1).join('\n').trim();}
 function formatted(value){return Number(value).toLocaleString('en-US',{minimumFractionDigits:3,maximumFractionDigits:3});}
 const expected=JSON.parse(fs.readFileSync('/reference/actual-AICU.json','utf8')).data;
 try{
  fs.mkdirSync(out,{recursive:true});const cdp=await page.createCDPSession();await cdp.send('Page.setDownloadBehavior',{behavior:'allow',downloadPath:out});
  const url=process.env.APP_URL;mark('GOTO_START',safeUrl(url));await page.goto(url,{waitUntil:'networkidle2',timeout:30000});mark('GOTO_DONE');
  await page.waitForSelector('[data-testid="stRadio"]');mark('RADIO_START');
  await page.evaluate(()=>{const r=[...document.querySelectorAll('[data-testid="stRadio"] label')].find(x=>x.innerText.includes('Năng lượng (BR-04)'));if(!r)throw Error('Energy radio absent');r.click();});
  await wait('Năng lượng trên sản lượng loại A');await select('Khoang · vụ năng lượng','AICU');await wait('agc-cycle-AICU');
  const primary=await snapshot('AICU assumed primary and five-cycle portfolio');
  assert.equal(metric(primary,'Năng lượng (kWh)'),formatted(expected.energy.official_total_kwh));
  assert.equal(metric(primary,'Sản lượng A (kg)'),formatted(expected.official_a_kg));
  assert.equal(metric(primary,'kWh/kg A'),formatted(expected.intensity_kwh_kg_a));
  assert(primary.text.includes('AGC_SHARED_BASIS_V1:v1')&&primary.text.includes('giả định chưa xác minh')&&primary.text.includes('1.536'));
  assert.equal(primary.charts,1);assert(primary.text.includes('Loại: agc-cycle-Reference'));
  await page.screenshot({path:out+'/aicu-primary.png',fullPage:true});
  await click('Xác minh lineage raw');await wait('Raw hash verified:');
  const energyLineage=await snapshot('Resource revision raw hash and file line verified');
  assert(energyLineage.text.includes('Resources.csv')&&energyLineage.text.includes('True'));
  await select('Revision nguồn energy / harvest A',expected.harvest_a[0].record.revision_id);
  await click('Xác minh lineage raw');await wait('Production.csv');
  const harvestLineage=await snapshot('Harvest A independent revision lineage verified');assert(harvestLineage.text.includes('True'));
  const files=[];
  for(const label of ['CSV summary năng lượng','CSV quantities năng lượng','CSV harvest A contributions',
   'CSV portfolio (gồm excluded)','JSON intensity (typed)','JSON portfolio (typed)'])files.push(await download(label));
  await select('Khoang · vụ năng lượng','Reference');await wait('agc-cycle-Reference');
  const reference=await snapshot('Reference unresolved excluded but energy retained');
  assert.equal(metric(reference,'kWh/kg A'),'NULL');assert.equal(metric(reference,'Sản lượng A (kg)'),'NULL');
  assert.notEqual(metric(reference,'Năng lượng (kWh)'),'NULL');
  assert(reference.text.includes('unresolved_quarantine_records')&&reference.text.includes('harvest_snapshot_incomplete'));
  files.push(await download('JSON intensity (typed)'));
  await page.screenshot({path:out+'/reference-excluded.png',fullPage:true});
  await select('Khoang · vụ năng lượng','AICU');await wait('agc-cycle-AICU');
  const replay=await snapshot('AICU replay stable after compartment change');assert.deepEqual(replay.metrics,primary.metrics);
  const hashes=Object.fromEntries(files.map(f=>[f,{bytes:fs.statSync(out+'/'+f).size,sha256:crypto.createHash('sha256').update(fs.readFileSync(out+'/'+f)).digest('hex')}]));
  fs.writeFileSync(out+'/session.json',JSON.stringify({generated_at:new Date().toISOString(),runtime:'Docker Chromium/Puppeteer 0.5CPU/768MiB',node:process.version,browser:await browser.version(),url:safeUrl(url),publication:expected.publication.publication_id,steps,downloads:hashes},null,2)+'\n');
  mark('BROWSER_PASS',JSON.stringify(hashes));
 }finally{mark('CLOSE_START');await Promise.race([browser.close(),pause(15000).then(()=>{throw Error('Close timeout');})]);mark('CLOSE_DONE');}
})().catch(e=>{console.error(e);process.exit(1)});
