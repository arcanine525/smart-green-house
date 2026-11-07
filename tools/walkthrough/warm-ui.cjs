/* Unrecorded native startup verification; recording starts after this phase. */
const {chromium,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
 const prep=path.resolve(process.argv[2]);const runtime=JSON.parse(fs.readFileSync(path.join(prep,'runtime.json'),'utf8'));
 if(runtime.backend!=='clickhouse'||runtime.purpose!=='isolated_walkthrough')throw Error('Native isolated context required');
 const browser=await chromium.launch();const started=Date.now();
 try{
  const context=await browser.newContext({viewport:{width:1920,height:1080}});
  await context.route('**/*',route=>{const url=new URL(route.request().url());return ['http:','https:'].includes(url.protocol)&&url.origin!==new URL(runtime.url).origin?route.abort():route.continue();});
  const page=await context.newPage();page.on('pageerror',e=>console.log('pageerror '+e.message));page.on('requestfailed',r=>console.log('requestfailed '+r.url()+' '+r.failure()?.errorText));
  const observer=setInterval(async()=>{const seconds=Math.round((Date.now()-started)/1000);try{await page.screenshot({path:path.join(prep,'startup-observation-'+seconds+'.png')});console.log(JSON.stringify({seconds,script_state:await page.getByTestId('stApp').getAttribute('data-test-script-state'),metrics:await page.getByTestId('stMetric').count(),exceptions:await page.getByTestId('stException').count(),body:(await page.locator('body').innerText()).slice(-2000)}));}catch(e){console.log('observation '+e.message);}},30000);observer.unref();
  await page.goto(runtime.url,{waitUntil:'domcontentloaded'});
  await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:600000});
  await expect(page.locator('body')).toContainText('clickhouse-warehouse-v1');
  await expect(page.locator('body')).toContainText(runtime.publication.slice(0,12));
  await expect(page.getByTestId('stException')).toHaveCount(0);
  await page.screenshot({path:path.join(prep,'native-startup.png')});
  fs.writeFileSync(path.join(prep,'native-startup.json'),JSON.stringify({status:'passed',backend:'clickhouse',
   publication:runtime.publication,external_http_blocked:true,elapsed_seconds:(Date.now()-started)/1000,
   note:'Unrecorded browser readiness; elapsed is this attempt only, not total cold startup or a warm query benchmark'},null,2)+'\n');
  clearInterval(observer);console.log('Native browser startup passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
