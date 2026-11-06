/* Browser smoke for the ClickHouse AGC water slice; video remains a T51 task. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const out=path.resolve(process.env.EVIDENCE_DIR||'tmp/clickhouse-water/browser');
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1920,height:1080},acceptDownloads:true});
  const page=await context.newPage();
  await page.goto(process.env.APP_URL||'http://127.0.0.1:18582',{waitUntil:'domcontentloaded'});
  await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:240000});
  assert((await page.locator('body').innerText()).includes('clickhouse-water-v1'));
  await page.getByTestId('stSelectbox').nth(1).getByRole('combobox').click();
  await page.getByRole('option',{name:/^Automatoes —/}).click();
  await page.getByText('788,85 L/m²',{exact:true}).waitFor({timeout:240000});
  await page.getByText('324,08 L/m²',{exact:true}).waitFor({timeout:10000});
  assert.equal(await page.getByTestId('stException').count(),0);
  const pending=page.waitForEvent('download');
  await page.getByRole('button',{name:'⬇ CSV ngày',exact:true}).click();
  const download=await pending;
  await download.saveAs(path.join(out,'browser-days.csv'));
  await page.getByRole('heading',{name:/Nước · tưới/}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(out,'browser-water.png'),fullPage:false});
  fs.writeFileSync(path.join(out,'browser-check.json'),JSON.stringify({status:'passed',
   browser:'Chromium via Playwright 1.65.0',url:page.url(),backend_code:'clickhouse-water-v1',
   compartment:'Automatoes',window:'full cycle',irrigation:'788,85 L/m²',drain:'324,08 L/m²',
   download:download.suggestedFilename(),screenshot:'browser-water.png'},null,2)+'\n');
 }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
