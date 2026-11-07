/* Real Playwright interaction + annotated video; never substitutes DOM/DB values. */
const {chromium,expect}=require('playwright/test');
const fs=require('node:fs'); const path=require('node:path'); const crypto=require('node:crypto');
const {execFileSync}=require('node:child_process');
const base=process.env.APP_URL||'http://127.0.0.1:18572';
const out=path.resolve(process.env.EVIDENCE_DIR||'tmp/walkthrough/videos');
const selected=(process.env.BRS||'01,02,03,04,05,06').split(',');
const pace=Number(process.env.PACE||1);
const PUBLICATION='df3e4bcbacab45a5b86fc37d2f53c00a';
const sha=f=>crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
const scriptSha=sha(__filename),lockfileSha=sha(path.join(__dirname,'package-lock.json'));
const titles={'01':'Khí hậu','02':'Tưới và drain','03':'EC / pH drain','04':'Năng lượng','05':'Thu hoạch A / B','06':'Chi phí và kịch bản giá'};
let page,context,dir,steps,downloads,started,br;
const pause=ms=>page.waitForTimeout(ms*pace);
async function settled(){
 await page.waitForTimeout(650);
 await page.waitForFunction(()=>{const app=document.querySelector('[data-testid="stApp"]');return !app||app.getAttribute('data-test-script-state')!=='running';},{},{timeout:180000});
 await page.locator('[data-testid="stException"]').waitFor({state:'hidden',timeout:1000});
}
async function annotate(title,detail){
 await page.evaluate(({title,detail,br})=>{
  let box=document.getElementById('walkthrough-caption');
  if(!box){box=document.createElement('div');box.id='walkthrough-caption';box.style.cssText='position:fixed;bottom:0;left:0;right:0;height:136px;box-sizing:border-box;padding:18px 34px;background:#102b24;color:#fff;z-index:2147483646;pointer-events:none;font-family:Arial,sans-serif;box-shadow:0 -3px 15px #0003';document.body.append(box);}
  box.replaceChildren();for(const [text,size,color] of [[`SMART GREENHOUSE · BR-${br} · WALKTHROUGH TỰ ĐỘNG PLAYWRIGHT`,15,'#b3d7c5'],[title,26,'#fff'],[detail,21,'#e0eae5']]){let el=document.createElement('div');el.textContent=text;el.style.cssText=`font-size:${size}px;color:${color};margin:0 0 6px;line-height:1.25`;box.append(el);}
  if(!document.getElementById('walkthrough-style')){let s=document.createElement('style');s.id='walkthrough-style';s.textContent='[data-testid="stMainBlockContainer"]{padding-bottom:180px!important} [data-testid="stSidebarContent"]{padding-bottom:160px!important}';document.head.append(s);}
 },{title,detail,br});
}
async function focus(loc){
 await loc.waitFor({state:'visible',timeout:180000});await loc.scrollIntoViewIfNeeded();
 // Scroll recording viewport so captions never cover the target. This changes presentation only.
 await loc.evaluate(el=>el.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'}));
 const box=await loc.boundingBox();if(!box)throw Error('No target bounds');
 await page.mouse.move(box.x+Math.min(box.width/2,180),box.y+box.height/2,{steps:12});
 await loc.evaluate(el=>{el.style.outline='3px solid #e4ab26';el.style.outlineOffset='3px';setTimeout(()=>{el.style.outline='';el.style.outlineOffset=''},1600)});
 await pause(650);
}
async function click(loc){await focus(loc);await loc.click();await settled();}
async function step(title,detail,action){
 const start=(Date.now()-started)/1000; console.log(`BR-${br} ${steps.length+1}: ${title}`);
 await annotate(`${steps.length+1}. ${title}`,detail);await pause(1800);
 if(action)await action();await settled();await pause(3800);
 const screenshot=`step-${String(steps.length+1).padStart(2,'0')}.png`;await page.screenshot({path:path.join(dir,screenshot)});
 const metrics=await page.locator('[data-testid="stMetric"]').allInnerTexts();
 steps.push({step:steps.length+1,title,detail,start_seconds:start,end_seconds:(Date.now()-started)/1000,screenshot,metrics});
 fs.writeFileSync(path.join(dir,'steps.json'),JSON.stringify(steps,null,2));
}
async function combo(label,value){const loc=page.getByRole('combobox',{name:label,exact:true});await focus(loc);await loc.fill(value);await pause(550);await page.getByRole('option',{name:new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'))}).first().click();await settled();}
async function optionIndex(label,index){const loc=page.getByRole('combobox',{name:label,exact:true});await focus(loc);await loc.click();const options=page.getByRole('option');await options.nth(index).waitFor();const labelValue=await options.nth(index).innerText();await options.nth(index).click();await settled();return labelValue;}
async function radio(value){await click(page.getByRole('radio',{name:value,exact:true}).locator('xpath=ancestor::label'));}
async function tab(name){await click(page.getByRole('tab',{name,exact:true}));}
async function expander(name){await click(page.locator('summary').filter({hasText:name}).first());}
async function dates(start,end){
 const labels=['day','month','year'];
 for(const [side,value] of [['start date',start],['end date',end]]){const [y,m,d]=value.split('-');for(const [i,text] of [d,m,y].entries()){const loc=page.getByRole('spinbutton',{name:new RegExp('^'+labels[i]+', .*'+side+'$')});await focus(loc);await loc.click();await loc.pressSequentially(text,{delay:90});}
 await page.getByRole('heading').first().click();await settled();}
}
async function metric(label,value){const loc=page.locator('[data-testid="stMetric"]').filter({has:page.getByText(label,{exact:true})});await expect(loc).toContainText(value,{timeout:180000});}
async function readMetrics(){await focus(page.locator('[data-testid="stMetric"]').first());}
async function chart(index=0){const loc=page.locator('[data-testid="stPlotlyChart"]').nth(index);await focus(loc);const box=await loc.boundingBox();await page.mouse.move(box.x+box.width*.55,box.y+Math.min(box.height*.48,220),{steps:20});await pause(1000);const legend=loc.locator('.legendtoggle').first();if(await legend.count()){await legend.click();await pause(1300);await legend.click();}await pause(800);}
async function download(label){const loc=page.getByRole('button',{name:label,exact:true});await focus(loc);const promise=page.waitForEvent('download',{timeout:60000});await loc.click();const d=await promise;const name=d.suggestedFilename();const target=path.join(dir,'downloads',name);await d.saveAs(target);if(await d.failure())throw Error(await d.failure());if(fs.statSync(target).size<20)throw Error('Empty export');downloads.push({file:'downloads/'+name,sha256:sha(target),bytes:fs.statSync(target).size});await settled();}
async function checkExportPublication(){const json=downloads.filter(x=>x.file.endsWith('.json'));if(!json.length)throw Error('Missing JSON export');for(const x of json){const text=fs.readFileSync(path.join(dir,x.file),'utf8');JSON.parse(text);if(!text.includes(PUBLICATION))throw Error('Export publication mismatch');}}
async function common(comp,label='Khoang · vụ'){
 await step('Mở màn hình '+titles[br],'Chọn BR ở thanh bên; các thao tác tiếp theo chạy trên bản sao dữ liệu AGC.',async()=>{await click(page.locator('[data-testid="stSidebar"]').getByText(new RegExp('BR-'+br+'\\)')).first());});
 await step('Xác nhận publication lịch sử','Một publication cố định cho bộ lọc, chỉ số, chi tiết và file xuất.',async()=>{const loc=page.getByRole('combobox',{name:/^Publication/}).first();await focus(loc);await loc.click();await pause(1400);await page.getByRole('option',{selected:true}).click();await settled();});
 await step('Chọn khoang và vụ '+comp,'Khoang/vụ xác định phạm vi phân tích; chọn lại sẽ cập nhật bộ lọc liên quan.',()=>combo(label,comp));
}
const flows={
 '02':async()=>{
 await common('Automatoes');
 await step('Đọc dữ liệu toàn vụ','Xem tổng tưới/drain, coverage và cảnh báo diện tích giả định.',readMetrics);
 await step('Khám phá biểu đồ ngày','Di chuyển trên biểu đồ; click chú giải để ẩn/hiện một chuỗi.',()=>chart());
 await step('Lọc ngày 02–03/03/2020','Nhập ngày bắt đầu và kết thúc; cả hai ngày đều thuộc cửa sổ.',()=>dates('2020-03-02','2020-03-03'));
 await step('Đối chiếu tổng và tỷ lệ paired','Tưới 9,36; drain 3,89 L/m²; tỷ lệ 41,56%; paired coverage 100%.',async()=>{await metric('Irrigation observed','9,36');await metric('Drain observed','3,89');await metric('Tỷ lệ drain (paired)','41,56');await readMetrics();});
 await step('Chọn rule demo có version','Rule phục vụ minh họa kiểm tra ngưỡng; không coi là khuyến cáo nông học.',()=>optionIndex('Rule kiểm tra (tùy chọn, version tường minh)',1));
 await step('Đọc tổng hợp theo tuần','Tỷ lệ tuần dùng tổng drain / tổng tưới trên cùng ngày hợp lệ.',()=>focus(page.getByRole('heading',{name:'Tổng hợp tuần (ISO)'})));
 await step('Chọn một ngày để xem chi tiết','Xem ngày 03/03 cùng giá trị, quality và basis đã pin.',()=>combo('Chọn ngày','2020-03-03'));
 await step('Mở lineage về CSV gốc','Đọc tên file, dòng nguồn và trạng thái xác minh checksum.',()=>expander('Lineage về raw'));
 await step('Xuất CSV ngày','File CSV dùng đúng cửa sổ và publication đang hiển thị.',()=>download('⬇ CSV ngày'));
 await step('Xuất tổng hợp tuần và JSON','Lưu bảng tuần và kết quả có metadata để kiểm tra lại.',async()=>{await download('⬇ CSV tuần');await download('⬇ JSON summary (v2)');await checkExportPublication();});
 },
 '01':async()=>{
 await common('IUACAAS');
 await step('Chọn rule khí hậu demo','Revision cố định; ngưỡng demo phải được phân biệt với chuẩn nông học.',()=>combo('Revision rule tường minh (tối đa một revision cho mỗi metric)','climate-demo-b01:IUACAAS:v1'));
 await step('Lọc các đợt nhiệt độ','Bộ lọc chỉ thay danh sách đợt; tổng KPI vẫn phản ánh phạm vi đã chọn.',()=>radio('temperature'));
 await step('Đọc KPI và coverage','Coverage biểu thị phần thời gian đủ điều kiện đánh giá theo rule.',readMetrics);
 await step('Chọn đợt dài nhất','Đáp án: 1.180 phút, từ 21/05 11:45 đến 22/05/2020 07:25.',async()=>{const loc=page.getByRole('combobox',{name:/Chọn đợt/});await focus(loc);await expect(loc).toHaveValue(/1180,00p/);await loc.click();await pause(1600);await page.getByRole('option',{selected:true}).click();});
 await step('Tương tác biểu đồ nhiệt độ','Đọc actual, cận rule và khoảng trống; hover để xem thời điểm.',()=>chart());
 await step('Mở nguồn của đỉnh lệch','Kiểm tra revision, dòng CSV và checksum raw của điểm đỉnh.',()=>expander('Lineage về raw (đỉnh của đợt)'));
 await step('So sánh basis realized','Realized và requested là hai bối cảnh khác nhau; không tự thay thế nhau.',()=>radio('Realized (VIP: t_heat_vip/t_ventlee_vip/co2_vip)'));
 await step('Trở lại basis requested','Giữ basis requested cho ví dụ cắt cửa sổ và các file xuất tiếp theo.',()=>radio('Requested (t_heat_sp/t_vent_sp/co2_sp)'));
 await step('Giới hạn cửa sổ 21–22/05/2020','Thời điểm gốc của đợt vẫn được giữ khi xem một phần vụ.',()=>dates('2020-05-21','2020-05-22'));
 await step('Đọc lại danh sách và trace sau lọc','Kiểm tra đợt dài nhất cùng cửa sổ đang chọn trước khi tải file.',async()=>{await focus(page.getByRole('combobox',{name:/Chọn đợt/}));await expect(page.getByRole('combobox',{name:/Chọn đợt/})).toHaveValue(/1180,00p/);});
 await step('Xuất summary và danh sách đợt','CSV giữ thời lượng, cửa sổ, rule và basis phục vụ đối soát.',async()=>{await download('⬇ CSV summary');await download('⬇ CSV đợt (đã lọc)');});
 await step('Xuất JSON kết quả có version','Kiểm tra publication của file trùng với publication đã chọn.',async()=>{await download('⬇ JSON result (typed)');await checkExportPublication();});
 },
 '03':async()=>{
 await common('Reference','Khoang · vụ drain');
 await step('Chọn profile EC/pH demo','Profile có version tường minh, được dùng để đánh giá drain process computer.',()=>combo('Profile EC/pH (version tường minh)','drain-demo-b03:Reference:v1'));
 await step('Lọc đợt EC','Đổi metric của danh sách; KPI EC/pH và OR vẫn được tính riêng.',()=>radio('ec'));
 await step('Đọc KPI EC và coverage','Phân biệt ngoài dải, suspect và thời gian không đánh giá được.',readMetrics);
 await step('Xem đợt EC dài nhất','Đáp án: 80.400 phút, từ 31/12/2019 12:35 đến 25/02/2020 08:35.',async()=>{const loc=page.getByRole('combobox',{name:'Chọn đợt để xem chi tiết',exact:true});await focus(loc);await expect(loc).toHaveValue(/80400/);await loc.click();await pause(1500);await page.getByRole('option',{selected:true}).click();});
 await step('Khám phá trace đợt EC','Hover actual/cận và kiểm tra phần trace được giới hạn khi đợt dài.',()=>chart());
 await step('Chuyển sang các đợt pH','Quan sát danh sách pH; không cộng thời lượng EC và pH để tính OR.',()=>radio('ph'));
 await step('Trở lại EC và chọn cửa sổ nhỏ','Xem 02–03/03/2020 để tra cứu dữ liệu nguồn theo ngày.',async()=>{await radio('ec');await dates('2020-03-02','2020-03-03');});
 await step('Chọn ngày nguồn','Ngày nguồn giúp đọc các slot đo gốc và quality EC/pH.',async()=>{for(const [part,value] of [['day','03'],['month','03'],['year','2020']]){const loc=page.getByRole('spinbutton',{name:part+', Ngày nguồn',exact:true});await focus(loc);await loc.click();await loc.pressSequentially(value,{delay:90});}await page.getByRole('heading').first().click();await settled();});
 await step('Chọn slot nguồn và xác minh lineage','Tên file, số dòng và checksum xác minh dữ liệu đến từ raw.',async()=>{await optionIndex('Dòng nguồn để truy vết',0);await click(page.getByRole('button',{name:'Xác minh lineage về raw',exact:true}));});
 await step('Xuất summary và đợt drain','File xuất đi cùng rule, phạm vi và publication đang xem.',async()=>{await download('CSV summary drain');await download('CSV đợt drain (đã lọc)');});
 await step('Xuất JSON kết quả drain','Kiểm tra file tải về chứa đúng publication đã pin.',async()=>{await download('JSON result drain (typed)');await checkExportPublication();});
 },
 '04':async()=>{
 await common('Automatoes','Khoang · vụ năng lượng');
 await step('Đọc năng lượng và cường độ toàn vụ','Mẫu số dùng kg loại A; kiểm tra basis và độ đầy đủ trước so sánh.',readMetrics);
 await step('Khám phá so sánh các khoang','Chỉ các khoang đủ điều kiện vào biểu đồ; Reference có thể bị loại.',()=>chart());
 await step('Mở công thức và đóng góp','Nhiệt MJ chuyển sang kWh; năng lượng và thu hoạch tổng hợp riêng trước ghép.',()=>expander('Công thức, basis và đóng góp'));
 await step('Lọc hai ngày 02–03/03/2020','Giữ năng lượng ngày chưa thu hoạch; không thay NULL bằng số 0.',()=>dates('2020-03-02','2020-03-03'));
 await step('Đối chiếu trường hợp mẫu số bằng 0','308,078 kWh; 0 kg A; kWh/kg A phải hiển thị NULL.',async()=>{await metric('Năng lượng (kWh)','308.078');await metric('Sản lượng A (kg)','0.000');await metric('kWh/kg A','NULL');await readMetrics();});
 await step('Chọn revision đóng góp','Chọn bản ghi năng lượng hoặc thu hoạch để kiểm tra nguồn.',()=>optionIndex('Revision nguồn energy / harvest A',0));
 await step('Xác minh lineage raw','Đọc checksum, file, dòng và giá trị nguồn trong kết quả truy vết.',()=>click(page.getByRole('button',{name:'Xác minh lineage raw',exact:true})));
 await step('Xuất summary và quantities','CSV giữ NULL, đơn vị và các điều kiện tính; không chỉ xuất các dòng đủ điểm.',async()=>{await download('CSV summary năng lượng');await download('CSV quantities năng lượng');});
 await step('Xuất JSON cường độ và portfolio','Đối soát publication và lưu cả các khoang bị loại khỏi so sánh.',async()=>{await download('JSON intensity (typed)');await download('JSON portfolio (typed)');await checkExportPublication();});
 },
 '05':async()=>{
 await common('Digilog');
 await step('Đọc sản lượng và lũy kế toàn vụ','Phân biệt tổng observed, paired và completeness của snapshot.',readMetrics);
 await step('Khám phá biểu đồ thu hoạch','Hover và ẩn/hiện chuỗi A/B, lũy kế; dữ liệu có grain record thu hoạch.',()=>chart());
 await step('Chọn ngưỡng loại B demo','Profile demo 10% được chọn theo version; không coi là tiêu chuẩn đã xác minh.',()=>combo('Rule kiểm tra (tùy chọn, version tường minh)','harvest-demo-b10:Digilog:v2'));
 await step('Lọc ngày 19/02/2020','Chọn cùng một ngày ở cả hai đầu để xem lần thu cụ thể.',()=>dates('2020-02-19','2020-02-19'));
 await step('Đối chiếu A, B và share B','A 0,05; B 0,10 kg/m²; share B 66,67%, vượt ngưỡng demo 10%.',async()=>{await metric('A observed','0,05');await metric('B observed','0,10');await metric('Share B (paired)','66,67');await readMetrics();});
 await step('Đổi grain từ record sang day','Kết luận rule áp dụng theo grain đang chọn, không nhầm record với trung bình.',()=>radio('ngày'));
 await step('Xem kết luận theo week','Tuần tổng hợp khối lượng; tỷ lệ dùng tổng tử/mẫu.',()=>radio('tuần (ISO)'));
 await step('Trở lại record và chọn lần thu','Xem danh tính source/revision, chất lượng A và B của lần thu.',async()=>{await radio('lần thu');await optionIndex('Chọn record',0);});
 await step('Mở lineage record accepted','Tên CSV, dòng và checksum xác minh record được dùng trong KPI.',()=>expander('Lineage về raw (record accepted)'));
 await step('Xuất lần thu và tổng hợp tuần','CSV phản ánh đúng cửa sổ; không tự nội suy ngày không có lần thu.',async()=>{await download('⬇ CSV lần thu');await download('⬇ CSV tuần');});
 await step('Xuất JSON có metadata','Đối chiếu publication trước khi dùng file làm bằng chứng.',async()=>{await download('⬇ JSON summary (window_summary)');await checkExportPublication();});
 },
 '06':async()=>{
 await common('Automatoes');
 await step('Lọc hai ngày 02–03/03/2020','Cửa sổ giá và lượng phải khớp khi so baseline với scenario.',()=>dates('2020-03-02','2020-03-03'));
 await step('Đọc baseline lịch sử','Baseline 17,60 EUR; diện tích 62,5 m² là giả định, chi phí không phải hóa đơn.',async()=>{await metric('Tổng chi phí (EUR)','17.60 EUR');await readMetrics();});
 await step('Xem giá có hiệu lực','Nhiệt 0,0083 EUR/MJ; điện cao/thấp 0,08/0,04 EUR/kWh.',()=>tab('📋 Biểu giá có hiệu lực'));
 await step('Mở phép nhân theo ngày','Đọc lượng × đơn giá của từng thành phần trong cửa sổ.',()=>tab('📅 Chi tiết ngày & Phép nhân'));
 await step('Đổi tổng hợp từ ngày sang tuần','Chọn grain tuần để xem tổng lượng và chi phí trong cùng cửa sổ.',()=>radio('Theo tuần (Weekly)'));
 await step('Trở lại grain ngày','Đối chiếu từng ngày trước khi mở nguồn của thành phần năng lượng.',()=>radio('Theo ngày (Daily)'));
 await step('Truy vết nguồn năng lượng','Chọn revision, đọc tên file và xác minh checksum quantity đầu vào.',async()=>{await expander('Truy vết nguồn gốc dữ liệu');await optionIndex('Chọn bản ghi năng lượng để truy vết nguồn (ngày · component · revision)',0);await expect(page.getByText('Xác minh hash raw', {exact:true}).locator('..')).toContainText('✅ Hợp lệ');});
 await step('Mở form tạo kịch bản','Giữ nguyên lượng tiêu thụ; chỉ thay giá điện trong ví dụ này.',()=>tab('⚙️ Tạo / Sửa đổi kịch bản'));
 const name='Walkthrough điện +20% '+new Date().toISOString().slice(0,19);
 await step('Đặt tên scenario mới','Tên rõ thay đổi để có thể tìm và mở lại đúng version.',async()=>{const loc=page.getByRole('textbox',{name:'Tên kịch bản',exact:true});await focus(loc);await loc.fill(name);});
 await step('Giữ giá nhiệt; tăng điện cao điểm 20%','Hình thức thay đổi theo phần trăm; nhập 20 vào trường điện cao điểm.',async()=>{await combo('Hình thức thay đổi điện cao điểm','Thay đổi theo phần trăm');const loc=page.getByRole('textbox',{name:'Phần trăm thay đổi điện cao điểm (%)',exact:true});await focus(loc);await loc.fill('20');});
 await step('Tăng điện thấp điểm 20%','Giá nhiệt giữ nguyên; giá hai thành phần điện cùng tăng 20%.',async()=>{await combo('Hình thức thay đổi điện thấp điểm','Thay đổi theo phần trăm');const loc=page.getByRole('textbox',{name:'Phần trăm thay đổi điện thấp điểm (%)',exact:true});await focus(loc);await loc.fill('20');});
 await step('Lưu kịch bản bất biến','Thao tác ghi chỉ diễn ra trong database demo riêng của buổi quay.',()=>click(page.getByRole('button',{name:'Lưu kịch bản bất biến',exact:true})));
 await step('Đọc baseline, scenario và chênh lệch','Baseline 17,60 → scenario 20,84 EUR; tăng 3,24 EUR với cùng lượng tiêu thụ.',async()=>{await focus(page.locator('[data-testid="stMetric"]').first());await expect(page.getByRole('combobox',{name:'Chọn kịch bản giá',exact:true})).toHaveValue(new RegExp(name.replace('+','\\+')));await metric('Baseline (EUR)','17.60 EUR');await metric('Kịch bản (EUR)','20.84 EUR');});
 await step('Khám phá đóng góp thành phần','Bảng chi phí nhiệt và điện đối chiếu baseline, scenario, delta và coverage.',async()=>{await tab('📊 Đóng góp thành phần');await focus(page.getByRole('tabpanel',{name:'📊 Đóng góp thành phần',exact:true}).locator('[data-testid="stDataFrame"]'));});
 await step('Bỏ chọn rồi mở lại scenario đã lưu','Mở lại đúng tên/version; kịch bản phục hồi cửa sổ và baseline đã pin.',async()=>{await optionIndex('Chọn kịch bản giá',0);await combo('Chọn kịch bản giá',name);await metric('Baseline (EUR)','17.60 EUR');await metric('Kịch bản (EUR)','20.84 EUR');});
 await step('Xuất kết quả kịch bản','CSV và JSON giữ version cùng điều kiện phân tích đã dùng.',async()=>{await tab('💾 Xuất dữ liệu (Exports)');await download('CSV Summary chi phí (Baseline vs Kịch bản)');await download('JSON Chi phí (ScenarioComparison typed)');await download('JSON Kịch bản DTO (CostScenario typed)');if(!downloads.length)throw Error('No scenario comparison export');await checkExportPublication();});
 }
};
(async()=>{
 const demo=JSON.parse(fs.readFileSync(process.env.DEMO_CONTEXT||path.join(out,'..','context.json'),'utf8'));if(!demo.database.startsWith('test_walkthrough_')||demo.publication!==PUBLICATION)throw Error('Expected prepared walkthrough copy');fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch();
 try{for(br of selected){if(!flows[br])throw Error('Unknown BR '+br);dir=path.join(out,'br-'+br);if(fs.existsSync(path.join(dir,'result.json')))throw Error('Use a new output directory; result exists: '+dir);fs.mkdirSync(path.join(dir,'downloads'),{recursive:true});steps=[];downloads=[];
 context=await browser.newContext({viewport:{width:1920,height:1080},recordVideo:{dir:path.join(dir,'recording'),size:{width:1920,height:1080}},acceptDownloads:true,locale:'vi-VN'});
 await context.tracing.start({screenshots:true,snapshots:true,sources:true});page=await context.newPage();await page.addInitScript(()=>{document.addEventListener('mousemove',e=>{let dot=document.getElementById('walkthrough-pointer');if(!dot){dot=document.createElement('div');dot.id='walkthrough-pointer';dot.style.cssText='position:fixed;width:22px;height:22px;border:3px solid #e4ab26;border-radius:50%;background:#e4ab2630;z-index:2147483647;pointer-events:none';document.body.append(dot);}dot.style.left=(e.clientX-11)+'px';dot.style.top=(e.clientY-11)+'px';});});page.setDefaultTimeout(60000);started=Date.now();let error;
 try{await page.goto(base);await page.getByText('Irrigation observed',{exact:true}).waitFor({timeout:180000});await settled();await flows[br]();await step('Hoàn tất BR-'+br,'Các bước đã kiểm tra tự động. Video minh họa thao tác; không đo khả năng sử dụng của người thật.',readMetrics);}
 catch(e){error=e;await page.screenshot({path:path.join(dir,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(dir,'failure.txt'),String(e.stack));}
 await context.tracing.stop({path:path.join(dir,'trace.zip')});const video=page.video();await context.close();const raw=await video.path();
 if(error)throw error;
 const mp4=path.join(dir,`br-${br}.mp4`);execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-y','-i',raw,'-an','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',mp4],{stdio:'inherit'});
 const probe=JSON.parse(execFileSync('ffprobe',['-v','error','-show_streams','-show_format','-of','json',mp4],{encoding:'utf8'}));if(probe.streams[0].width!==1920||probe.streams[0].height!==1080)throw Error('Wrong video dimensions');
 fs.writeFileSync(path.join(dir,'result.json'),JSON.stringify({br,title:titles[br],status:'passed',kind:'automated_playwright_walkthrough',script_sha256:scriptSha,lockfile_sha256:lockfileSha,human_participants:0,demo_database:demo.database,publication:PUBLICATION,playwright:require('playwright/package.json').version,commit:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),video:{file:`br-${br}.mp4`,sha256:sha(mp4),width:1920,height:1080,duration_seconds:Number(probe.format.duration),audio:false},steps,downloads},null,2));
 console.log('PASS BR-'+br+' '+mp4);
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
