/* Verify the local player and chapter navigation against the actual MP4 files. */
const {chromium,expect}=require('playwright/test');
const fs=require('node:fs'),path=require('node:path');
const {pathToFileURL}=require('node:url');
(async()=>{
 const root=path.resolve(process.argv[2]);
 const output=path.resolve(process.argv[3]);
 if(fs.existsSync(output))throw Error('Preserve previous player verification');
 const summary=JSON.parse(fs.readFileSync(path.join(root,'summary.json'),'utf8'));
 const browser=await chromium.launch();
 const results=[];
 try{
  const page=await browser.newPage({viewport:{width:1920,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(pathToFileURL(path.join(root,'index.html')).href);
  await expect(page.locator('video')).toHaveCount(summary.videos.length);
  for(const item of summary.videos){
   const video=page.locator('#v'+item.br);
   const chapter=item.chapters[2];
   await video.locator('xpath=ancestor::section').getByRole('button').nth(2).click();
   await expect.poll(()=>video.evaluate(v=>v.readyState)).toBeGreaterThanOrEqual(2);
   await expect.poll(()=>video.evaluate(v=>v.paused)).toBe(false);
   await expect.poll(()=>video.evaluate(v=>v.currentTime)).toBeGreaterThan(chapter.start_seconds);
   const state=await video.evaluate(v=>({width:v.videoWidth,height:v.videoHeight,
    currentTime:v.currentTime,duration:v.duration,error:v.error?.message||null}));
   if(state.width!==1920||state.height!==1080||state.error)throw Error('Unplayable video '+item.br);
   await video.evaluate(v=>v.pause());
   results.push({br:item.br,chapter:chapter.step,requested_seconds:chapter.start_seconds,...state});
  }
  if(errors.length)throw Error(errors.join('\n'));
  fs.mkdirSync(path.dirname(output),{recursive:true});
  fs.writeFileSync(output,JSON.stringify({status:'passed',backend:summary.backend,
   artifact_root:root,local_file_player:true,chapter_navigation:true,videos:results},null,2)+'\n');
  console.log('All local players and chapters passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
