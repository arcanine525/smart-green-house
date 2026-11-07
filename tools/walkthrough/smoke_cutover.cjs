/* Read-only browser smoke for the real native/rollback deployment. */
const {chromium,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path');
const [url,backend,publication,replay,outArg,mode]=process.argv.slice(2);
if(!url||!['clickhouse','postgres'].includes(backend)||!publication||!outArg)throw Error('url backend publication replay output required');
if(mode&&mode!=='water-only')throw Error('Unknown smoke mode');
const out=path.resolve(outArg);fs.mkdirSync(out,{recursive:true});
if(fs.existsSync(path.join(out,'result.json')))throw Error('Preserve previous smoke result');
const screens=[['water','💧 Nước (BR-02)','💧 Nước · tưới & drain'],
 ['harvest','🍅 Thu hoạch (BR-05)','🍅 Thu hoạch · sản lượng & cơ cấu A/B'],
 ['climate','🌡️ Khí hậu (BR-01)','🌡️ Khí hậu · các đợt lệch điều kiện'],
 ['drain','🧪 EC/pH drain (BR-03)','EC/pH dung dịch drain'],
 ['energy','⚡ Năng lượng (BR-04)','Năng lượng trên sản lượng loại A'],
 ['cost','💶 Chi phí & Kịch bản (BR-06)','Chi phí năng lượng & Kịch bản biểu giá'],
 ['overview','🏠 Tổng quan vụ','🏠 Tổng quan vụ'],
 ['quality','🛡️ Chất lượng dữ liệu','🛡️ Chất lượng dữ liệu'],
 ['replay','📡 Sensor replay (BR-01/03)','📡 Sensor replay · BR-01 / BR-03']];
(async()=>{
 const start=Date.now(),browser=await chromium.launch(),checks=[];
 const context=await browser.newContext({viewport:{width:1600,height:1000}});
 await context.route('**/*',r=>{const u=new URL(r.request().url());return ['http:','https:'].includes(u.protocol)&&u.origin!==new URL(url).origin?r.abort():r.continue();});
 const page=await context.newPage();
 async function settled(){
  await page.waitForFunction(()=>document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')==='notRunning',null,{timeout:600000});
  await expect(page.getByTestId('stException')).toHaveCount(0);
  const errors=page.getByTestId('stAlert').filter({has:page.locator('[data-testid="stIconMaterial"]').filter({hasText:'error'})});
  await expect(errors).toHaveCount(0);
 }
 try{
  await page.goto(url,{waitUntil:'domcontentloaded'});
  await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:600000});await settled();
  for(const [name,label,title] of screens){
   await page.getByText(label,{exact:true}).click();
   await page.getByRole('heading',{name:title,exact:true}).waitFor({timeout:600000});await settled();
   if(name==='replay'){
    const box=page.getByRole('combobox',{name:'Replay · khoang',exact:true});
    await box.fill(replay);await page.getByRole('option').filter({hasText:replay}).first().click();await settled();
    await expect(page.locator('body')).toContainText('Replay đã đối soát');
    await expect(page.getByTestId('stJson').first()).toContainText(/Sequence hợp lệ[\s"':]*30/);
    if(process.env.SMOKE_EXPECT_REPLAY_CLOCK){
     const clock=process.env.SMOKE_EXPECT_REPLAY_CLOCK.replace(/[.*+?^${}()|[\]\\]/g,'\\$&').replace('T','[T ]');
     await expect(page.locator('body')).toContainText(new RegExp('Replay clock \\(source\\): '+clock));
    }
   }else{
    await expect(page.locator('body')).toContainText(publication.slice(0,12));
    if(backend==='clickhouse'&&name==='water')await expect(page.locator('body')).toContainText('clickhouse-warehouse-v1');
    // React may finish painting the lower page after the backend switches to
    // notRunning. Wait for the actual export control instead of sampling 0.
    if(!['overview','quality'].includes(name))await page.getByTestId('stDownloadButton').first().waitFor({state:'attached',timeout:120000});
   }
   const metrics=await page.getByTestId('stMetric').allInnerTexts();
   checks.push({screen:name,metrics,publication:name==='replay'?null:publication});
   await page.screenshot({path:path.join(out,name+'.png')});console.log(JSON.stringify({screen:name,status:'passed'}));
   if(mode==='water-only')break;
  }
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({status:'passed',backend,url,publication,replay,
   external_http_blocked:true,read_only_actions:true,mode:mode||'all-nine-screens',
   expected_replay_clock:process.env.SMOKE_EXPECT_REPLAY_CLOCK||null,
   elapsed_seconds:(Date.now()-start)/1000,checks},null,2)+'\n');
 }catch(error){
  fs.writeFileSync(path.join(out,'failed-body.txt'),await page.locator('body').innerText().catch(()=>''));
  await page.screenshot({path:path.join(out,'failed.png')}).catch(()=>{});throw error;
 }
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
