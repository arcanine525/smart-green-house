/* Production ClickHouse navigation smoke; the detailed six videos belong to T51. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.env.EVIDENCE_DIR||'tmp/clickhouse-ui-browser');
const publication=process.env.PUBLICATION_ID;
assert(publication,'PUBLICATION_ID must explicitly pin the expected source');
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1920,height:1080},acceptDownloads:true});
  const page=await context.newPage(),checks=[];
  const settled=async()=>{
   await page.waitForTimeout(400);
   await page.waitForFunction(()=>document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')==='notRunning',null,{timeout:240000});
   assert.equal(await page.getByTestId('stException').count(),0);
   assert.equal(await page.getByTestId('stAlert').filter({has:page.locator('[data-testid="stIconMaterial"]').filter({hasText:'error'})}).count(),0);
  };
  await page.goto(process.env.APP_URL||'http://127.0.0.1:18582',{waitUntil:'domcontentloaded'});
  await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:240000});await settled();
  await page.getByTestId('stSelectbox').nth(1).getByRole('combobox').click();
  await page.getByRole('option',{name:/^Automatoes —/}).click();
  await page.getByText('788,85 L/m²',{exact:true}).waitFor({timeout:240000});await settled();
  const screens=[['water','💧 Nước (BR-02)'],['harvest','🍅 Thu hoạch (BR-05)'],['climate','🌡️ Khí hậu (BR-01)'],
   ['drain','🧪 EC/pH drain (BR-03)'],['energy','⚡ Năng lượng (BR-04)'],['cost','💶 Chi phí & Kịch bản (BR-06)'],
   ['overview','🏠 Tổng quan vụ'],['quality','🛡️ Chất lượng dữ liệu'],['replay','📡 Sensor replay (BR-01/03)']];
  const titles={water:'💧 Nước · tưới & drain',harvest:'🍅 Thu hoạch · sản lượng & cơ cấu A/B',
   climate:'🌡️ Khí hậu · các đợt lệch điều kiện',drain:'EC/pH dung dịch drain',energy:'Năng lượng trên sản lượng loại A',
   cost:'Chi phí năng lượng & Kịch bản biểu giá',overview:'🏠 Tổng quan vụ',quality:'🛡️ Chất lượng dữ liệu',replay:'📡 Sensor replay · BR-01 / BR-03'};
  for(const [name,label] of screens){
   await page.getByText(label,{exact:true}).click();
   await page.getByRole('heading',{name:titles[name],exact:true}).waitFor({timeout:240000});await settled();
   const body=await page.locator('body').innerText();
   if(name==='replay')assert(body.includes('Chưa có replay publication'),'Expected explicit replay empty state');
   else assert(body.includes(publication.slice(0,12)),'Wrong publication in '+name);
   const downloads=await page.getByTestId('stDownloadButton').count();
   if(!['overview','quality','replay'].includes(name))assert(downloads>0,'Missing export '+name);
   if(name==='quality'){
    await page.locator('summary').filter({hasText:'Chi tiết measure'}).click();
    await page.getByText('Tải chi tiết measure và CSV',{exact:true}).click();await settled();
    const button=page.getByRole('button',{name:'⬇ CSV chi tiết chất lượng',exact:true});
    if(await button.count()){
     const pending=page.waitForEvent('download');await button.click();
     await (await pending).saveAs(path.join(out,'quality-details.csv'));
    }else assert((await page.locator('body').innerText()).includes('Không có quyết định measure bị loại hoặc chưa chắc chắn'),
     'Quality details must provide CSV or an explicit empty state');
   }
   checks.push({screen:name,publication:name==='replay'?null:publication,downloads,
    metrics:await page.getByTestId('stMetric').allInnerTexts()});
   if(['overview','quality','replay'].includes(name))await page.screenshot({path:path.join(out,name+'.png')});
   console.log('passed '+name);
  }
  fs.writeFileSync(path.join(out,'browser-check.json'),JSON.stringify({status:'passed',browser:'Chromium via Playwright 1.65.0',
   backend:'clickhouse',historical_scope:'AGC full Resources/Production and 02–03 March climate rehearsal; full backfill remains T38',
   replay:'empty state; populated replay/rule/refresh tested by production AppTest with exact simulator outbox bytes',checks},null,2)+'\n');
 }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
