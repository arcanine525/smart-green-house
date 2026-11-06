const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const crypto=require('crypto');
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const root=path.resolve(process.argv[2]);
const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const records=['01','02','03','04','05','06'].map(br=>{
 const dir=path.join(root,`br-${br}`);
 const r=JSON.parse(fs.readFileSync(path.join(dir,'result.json'),'utf8'));
 assert.equal(r.br,br);assert.equal(r.status,'passed');assert.equal(r.video.width,1920);assert.equal(r.video.height,1080);assert.equal(r.video.audio,false);assert(r.steps.length>=10);
 assert.equal(sha(path.join(dir,r.video.file)),r.video.sha256);
 for(const download of r.downloads)assert.equal(sha(path.join(dir,download.file)),download.sha256);
 for(const step of r.steps)assert(fs.existsSync(path.join(dir,step.screenshot)));
 return r;
});
fs.writeFileSync(path.join(root,'index.html'),`<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Smart Greenhouse · 6 BR walkthrough</title><style>body{font:17px system-ui;background:#f3f5f0;color:#173d30;margin:0;padding:35px;max-width:1400px;margin:auto}h1{font-size:32px}section{background:white;border:1px solid #d7e0d4;border-radius:16px;margin:24px 0;padding:24px}video{width:100%;max-height:720px;background:#102b24}button{font:inherit;text-align:left;color:#173d30;background:#edf3e6;border:0;padding:10px;margin:4px;border-radius:7px;cursor:pointer}.steps{display:grid;grid-template-columns:1fr 1fr}p{line-height:1.6}@media(max-width:800px){.steps{grid-template-columns:1fr}}</style><h1>Smart Greenhouse · Walkthrough sáu BR</h1><p>Playwright thao tác trên dashboard thật, dữ liệu lịch sử AGC và cấu hình demo. Video 1080p, chú thích tiếng Việt, không âm thanh. Đây là minh chứng tự động; không có quan sát người tham gia.</p>${records.map(r=>`<section><h2>BR-${r.br} · ${escape(r.title)}</h2><p>${r.steps.length} bước · ${(r.video.duration_seconds/60).toFixed(1)} phút · <a href="br-${r.br}/${r.video.file}" download>Tải MP4</a> · <a href="br-${r.br}/result.json">Kết quả và checksum</a></p><video id="v${r.br}" controls preload="metadata" src="br-${r.br}/${r.video.file}"></video><div class="steps">${r.steps.map(s=>`<button onclick="const v=document.getElementById('v${r.br}');v.currentTime=${s.start_seconds};v.play()">${s.step}. ${escape(s.title)}</button>`).join('')}</div></section>`).join('')}</html>`);
fs.writeFileSync(path.join(root,'summary.json'),JSON.stringify({kind:'automated_playwright_walkthrough',artifact_root:path.relative(process.cwd(),root),human_participants:0,videos:records.map(({steps,...r})=>({...r,step_count:steps.length,chapters:steps.map(({step,title,start_seconds})=>({step,title,start_seconds}))}))},null,2));
console.log(path.join(root,'index.html'));
