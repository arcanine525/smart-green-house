/* Unrecorded, read-only rehearsal on the real UI. No DOM/data substitution. */
const {chromium,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [input,outArg]=process.argv.slice(2),config=JSON.parse(fs.readFileSync(input,'utf8'));
const out=path.resolve(outArg),url=new URL(config.url);
if(!['127.0.0.1','localhost'].includes(url.hostname))throw Error('Local rehearsal URL required');
if(fs.existsSync(out))throw Error('Preserve prior rehearsal output; choose a new path');
fs.mkdirSync(path.join(out,'downloads'),{recursive:true});
const sha=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const start=Date.now(),steps=[],downloads=[];let page,activeWindow;
async function settled(){
 await page.waitForTimeout(850);
 await page.waitForFunction(()=>document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')==='notRunning',null,{timeout:600000});
 await expect(page.getByTestId('stException')).toHaveCount(0);
 const errors=page.getByTestId('stAlert').filter({has:page.locator('[data-testid="stIconMaterial"]').filter({hasText:'error'})});
 await expect(errors).toHaveCount(0);
}
async function combo(label,value){
 const box=page.getByRole('combobox',{name:label,exact:true});
 await box.scrollIntoViewIfNeeded();await box.click();await box.fill(value);await page.waitForTimeout(650);
 await page.getByRole('option').filter({hasText:value}).first().click();await settled();
}
async function screen(label,title){
 await page.getByText(label,{exact:true}).click();
 await page.getByRole('heading',{name:title,exact:true}).waitFor({timeout:600000});await settled();
}
async function dates(start,end){
 const labels=['day','month','year'];
 const part=(side,label)=>page.getByRole('spinbutton',{name:new RegExp('^'+label+', .*'+side+'$')});
 async function current(side){
  const values=await Promise.all(labels.map(async label=>{
   const value=await part(side,label).getAttribute('aria-valuenow');
   if(value===null)throw Error('Missing accessible date field value');return Number(value);
  }));
  return String(values[2]).padStart(4,'0')+'-'+String(values[1]).padStart(2,'0')+'-'+String(values[0]).padStart(2,'0');
 }
 // Moving a range forward past its old end: extend the end first. Otherwise
 // React Aria correctly rejects start > end and restores the previous start.
 const forward=start>await current('end date');
 const edits=forward?[['end date',end],['start date',start]]:[['start date',start],['end date',end]];
 for(const [side,value] of edits){
  const [y,m,d]=value.split('-');
  for(const [i,text] of [d,m,y].entries()){
   const loc=part(side,labels[i]);
   await loc.click();await loc.pressSequentially(text,{delay:80});
  }
  await page.getByRole('heading').first().click();await settled();
 }
 if(await current('start date')!==start||await current('end date')!==end)throw Error('Date range did not commit exactly');
 const until=new Date(end+'T00:00:00Z');until.setUTCDate(until.getUTCDate()+1);
 activeWindow={start,end:until.toISOString().slice(0,10)};
}
async function metric(label,text){await expect(page.getByTestId('stMetric').filter({has:page.getByText(label,{exact:true})})).toContainText(text,{timeout:15000});}
async function exportFile(label,pub){
 const event=page.waitForEvent('download',{timeout:120000});await page.getByRole('button',{name:label,exact:true}).click();
 const d=await event;if(await d.failure())throw Error(await d.failure());
 const target=path.join(out,'downloads',String(downloads.length+1)+'-'+d.suggestedFilename());await d.saveAs(target);
 const text=fs.readFileSync(target,'utf8');if(text.length<30||!text.includes(pub))throw Error('Empty or wrong-publication export');
 if(target.endsWith('.json')){
  const data=JSON.parse(text),contexts=[];
  function visit(value){
   if(!value||typeof value!=='object')return;
   if(value.publication_id&&value.start&&value.end)contexts.push(value);
   for(const child of Object.values(value))visit(child);
  }
  visit(data);
  if(activeWindow&&!contexts.some(c=>c.publication_id===pub&&c.start===activeWindow.start&&c.end===activeWindow.end))throw Error('Export date/publication context differs from selected window');
 }
 downloads.push({file:path.relative(out,target),sha256:sha(target),bytes:fs.statSync(target).size,publication:pub});await settled();
}
async function step(title,action){
 const t=Date.now();await action();await settled();
 const file='step-'+String(steps.length+1).padStart(2,'0')+'.png';await page.screenshot({path:path.join(out,file)});
 steps.push({title,seconds:(Date.now()-t)/1000,screenshot:file,metrics:await page.getByTestId('stMetric').allInnerTexts()});
 fs.writeFileSync(path.join(out,'steps.json'),JSON.stringify(steps,null,2)+'\n');console.log(JSON.stringify({step:steps.length,title,status:'passed'}));
}
(async()=>{
 const browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1600,height:1000}});
 await context.route('**/*',r=>{const u=new URL(r.request().url());return ['http:','https:'].includes(u.protocol)&&u.origin!==url.origin?r.abort():r.continue();});
 page=await context.newPage();page.setDefaultTimeout(30000);
 try{
  await page.goto(config.url);await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:600000});await settled();
  await step('BR-02: current publication and corrected two-day water',async()=>{
   await combo('Khoang · vụ','Automatoes');await dates('2020-03-02','2020-03-03');
   await expect(page.locator('body')).toContainText(config.publication.slice(0,12));
   await metric('Irrigation observed','10,36');await metric('Drain observed','3,89');await metric('Tỷ lệ drain (paired)','37,55');
   await exportFile('⬇ CSV ngày',config.publication);await exportFile('⬇ JSON summary (v2)',config.publication);
  });
  await step('BR-01: climate rule and episode over boundary',async()=>{
   await screen('🌡️ Khí hậu (BR-01)','🌡️ Khí hậu · các đợt lệch điều kiện');await combo('Khoang · vụ','IUACAAS');
   await combo('Revision rule tường minh (tối đa một revision cho mỗi metric)','climate-demo-b01:IUACAAS:v1');
   await dates('2020-05-21','2020-05-22');
   await page.getByRole('radio',{name:'temperature',exact:true}).locator('xpath=ancestor::label').click();await settled();
   await expect(page.getByRole('combobox',{name:/Chọn đợt/})).toHaveValue(/1180,00p/);
   await exportFile('⬇ JSON result (typed)',config.publication);
  });
  await step('BR-03: drain rule, EC/pH and export',async()=>{
   await screen('🧪 EC/pH drain (BR-03)','EC/pH dung dịch drain');await combo('Khoang · vụ drain','Reference');
   await combo('Profile EC/pH (version tường minh)','drain-demo-b03:Reference:v1');await dates('2020-03-02','2020-03-03');
   await exportFile('JSON result drain (typed)',config.publication);
  });
  await step('BR-05: harvest source day and paired share',async()=>{
   await screen('🍅 Thu hoạch (BR-05)','🍅 Thu hoạch · sản lượng & cơ cấu A/B');await combo('Khoang · vụ','Digilog');
   await dates('2020-02-19','2020-02-19');await metric('A observed','0,05');await metric('B observed','0,10');await metric('Share B (paired)','66,67');
   await exportFile('⬇ JSON summary (window_summary)',config.publication);
  });
  await step('BR-04: energy and zero A denominator',async()=>{
   await screen('⚡ Năng lượng (BR-04)','Năng lượng trên sản lượng loại A');await combo('Khoang · vụ năng lượng','Automatoes');
   await dates('2020-03-02','2020-03-03');await metric('Năng lượng (kWh)','308.078');await metric('Sản lượng A (kg)','0.000');await metric('kWh/kg A','NULL');
   await exportFile('JSON intensity (typed)',config.publication);
  });
  await step('BR-06: baseline quantities and historical tariff',async()=>{
   await screen('💶 Chi phí & Kịch bản (BR-06)','Chi phí năng lượng & Kịch bản biểu giá');await combo('Khoang · vụ','Automatoes');
   await dates('2020-03-02','2020-03-03');await metric('Tổng chi phí (EUR)','17.60 EUR');
   await page.getByRole('tab',{name:'📋 Biểu giá có hiệu lực',exact:true}).click();await settled();
  });
  if(config.scenario)await step('BR-06: reopen saved scenario with explicit original frozen context',async()=>{
   await combo('Chọn kịch bản giá',config.scenario.name);await metric('Baseline (EUR)','17.60 EUR');await metric('Kịch bản (EUR)','20.84 EUR');
   await expect(page.locator('body')).toContainText('Kịch bản được pin với ngữ cảnh phân tích gốc');
   await expect(page.locator('body')).toContainText(config.scenario.publication);
   await page.getByRole('tab',{name:'💾 Xuất dữ liệu (Exports)',exact:true}).click();await settled();
   await exportFile('JSON Chi phí (ScenarioComparison typed)',config.scenario.publication);
   // Restore baseline without creating a scenario or changing persistence.
   const box=page.getByRole('combobox',{name:'Chọn kịch bản giá',exact:true});await box.fill('Baseline tham chiếu');
   await page.getByRole('option').first().click();await settled();await metric('Tổng chi phí (EUR)','17.60 EUR');
  });
  await step('Overview and quality on current publication',async()=>{
   await screen('🏠 Tổng quan vụ','🏠 Tổng quan vụ');await expect(page.locator('body')).toContainText(config.publication.slice(0,12));
   await screen('🛡️ Chất lượng dữ liệu','🛡️ Chất lượng dữ liệu');await expect(page.locator('body')).toContainText(config.publication.slice(0,12));
  });
  await step('Sensor: complete 30-event pin and source clock',async()=>{
   await screen('📡 Sensor replay (BR-01/03)','📡 Sensor replay · BR-01 / BR-03');await combo('Replay · khoang',config.replay);
   await expect(page.locator('body')).toContainText('Replay đã đối soát manifest, checksum và sequence.');
   await expect(page.getByTestId('stJson').first()).toContainText(/Sequence hợp lệ[\s"':]*30/);
   await expect(page.locator('body')).toContainText(/Replay clock \(source\): 2019-12-16[T ]02:25:00/);
  });
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({status:'passed',kind:'unrecorded_read_only_rehearsal',config,script_sha256:sha(__filename),external_http_blocked:true,no_database_writes:true,recorded_video:false,elapsed_seconds:(Date.now()-start)/1000,steps,downloads,script_target_minutes:24,spoken_presentation_timing:'estimated, user records offline'},null,2)+'\n');
 }catch(error){
  fs.writeFileSync(path.join(out,'failed-body.txt'),await page.locator('body').innerText().catch(()=>''));await page.screenshot({path:path.join(out,'failed.png')}).catch(()=>{});throw error;
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
