/* Real browser smoke for the ClickHouse harvest screen; not the T51 video set. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.env.EVIDENCE_DIR||'tmp/clickhouse-harvest-browser');
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1920,height:1080},acceptDownloads:true});
  const page=await context.newPage();
  await page.goto(process.env.APP_URL||'http://127.0.0.1:18582',{waitUntil:'domcontentloaded'});
  await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:240000});
  await page.getByText('🍅 Thu hoạch (BR-05)',{exact:true}).click();
  await page.getByText('A observed',{exact:true}).waitFor({timeout:240000});
  await page.getByTestId('stSelectbox').nth(1).getByRole('combobox').click();
  await page.getByRole('option',{name:/^Digilog —/}).click();
  await page.getByText('13,51 kg/m²',{exact:true}).waitFor({timeout:240000});
  await page.getByText('0,70 kg/m²',{exact:true}).waitFor({timeout:10000});
  await page.waitForFunction(()=>document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')!=='running',{},{timeout:240000});
  const body=await page.locator('body').innerText();
  assert(body.includes('clickhouse-warehouse-v1'));
  assert.equal(await page.getByTestId('stException').count(),0);
  const buttons=page.getByTestId('stDownloadButton');
  const labels=await buttons.allTextContents();
  const index=labels.findIndex(s=>s.includes('CSV lần thu'));
  assert(index>=0,'Missing records export');
  const pending=page.waitForEvent('download');
  await buttons.nth(index).getByRole('button').click();
  const download=await pending; await download.saveAs(path.join(out,'harvest-records.csv'));
  await page.getByRole('heading',{name:/Thu hoạch · sản lượng/}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(out,'harvest-digilog.png')});
  await page.getByTestId('stSelectbox').nth(1).getByRole('combobox').click();
  await page.getByRole('option',{name:/^Reference —/}).click();
  await page.getByText('snapshot: chưa hoàn chỉnh',{exact:true}).waitFor({timeout:240000});
  assert.equal(await page.getByTestId('stException').count(),0);
  await page.getByRole('heading',{name:/Thu hoạch · sản lượng/}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(out,'harvest-reference.png')});
  fs.writeFileSync(path.join(out,'browser-check.json'),JSON.stringify({status:'passed',backend:'clickhouse-warehouse-v1',
   screens:['Digilog harvest','Reference incomplete snapshot'],digilog_full_cycle:{prod_a:'13,51 kg/m²',prod_b:'0,70 kg/m²'},download:download.suggestedFilename(),labels},null,2)+'\n');
 }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
