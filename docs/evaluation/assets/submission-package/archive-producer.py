from pathlib import Path
import json,hashlib,shutil,subprocess,re
root=Path.cwd();local=root/'tmp/submission-rehearsal';run=local/'run-04'
def sha(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
result=json.loads((run/'result.json').read_text())
assert result['status']=='passed' and len(result['steps'])==9 and len(result['downloads'])==7
assert result['script_sha256']==sha(root/'tools/walkthrough/rehearse_submission.cjs')
assert result['config']['publication']=='5d455686a06649e789908b9708a9d436'
assert result['config']['scenario']['publication']=='4b08bd51c092465783304f887d766d47'
assert (local/'pre-video-final.log').read_text().find('PRE_VIDEO_PASS')>=0
assert 'FAIL: Fill member names/student IDs' in (local/'strict-final-check.log').read_text()
source_check=json.loads((local/'source-package-final.json').read_text());assert source_check['status']=='passed'
assert source_check['zip_sha256']==sha(root/'project-submit/29_application.zip')
out=root/'docs/evaluation/assets/submission-package';out.mkdir(parents=True,exist_ok=False)
originals={}
def copy(src,target):
 p=local/src;q=out/target;q.parent.mkdir(parents=True,exist_ok=True);originals[str(src)]=sha(p)
 if p.suffix=='.log':
  t=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',p.read_text());q.write_text('\n'.join(s.rstrip() for s in t.splitlines())+'\n')
 else:shutil.copy2(p,q)
for p in sorted(run.rglob('*')):
 if p.is_file():copy(p.relative_to(local),Path('rehearsal')/p.relative_to(run))
for label in ('01','02','03'):
 for name in ('failed.png','failed-body.txt'):
  copy('run-'+label+'/'+name,Path('earlier-harness-failures')/('run-'+label)/name)
 copy('run-'+label+'.log',Path('earlier-harness-failures')/('run-'+label)/'run.log')
for name in ('pre-video-final.log','strict-final-check.log','tamper-rejection.log','stale-source-rejection.log','source-package-final.json','source-package-final.log','read-only-journal.json'):
 copy(name,name)
shutil.copy2(root/'tools/walkthrough/rehearse_submission.cjs',out/'executed-rehearsal.cjs')
shutil.copy2(local/'check_source_archive_final.py',out/'source-archive-check.py')
shutil.copy2(local/'archive_final.py',out/'archive-producer.py')
shutil.copy2(root/'project-submit/29_delivery_manifest.json',out/'delivery-manifest.json')
summary=dict(status='passed',kind='pre_video_acceptance_only',source_base_head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),native_publication=result['config']['publication'],frozen_scenario_publication=result['config']['scenario']['publication'],rehearsal_steps=len(result['steps']),unique_ui_screens=9,exports=len(result['downloads']),rehearsal_seconds=result['elapsed_seconds'],business_writes=False,journal_unchanged=True,script_target_minutes=24,human_spoken_timing='not measured; user records offline',source_package=source_check,overall_presentation='pending_user_offline',identity='three placeholders authorized by user',strict_final_gate='correctly rejected identity placeholders; video also absent',negative_controls=['corrupted artifact rejected','stale source snapshot rejected'],log_normalization='ANSI/CR/trailing whitespace only; originals remain local',original_log_and_file_sha256=originals)
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
(out/'README.md').write_text('''# T30 — Gói trước video và diễn tập native\n\n**PRE_VIDEO_PASS**, không phải gói đủ bốn sản phẩm. Report8trangA4/slide20trang,\neditable/script24phút, sourceZIP và bảy video native/21exports đã kiểm. Danh tính\nba thành viên còn placeholder theo user; video thuyết trình20–30phút user quay offline.\nChế độ checker cuối từ chối đúng; không tạo video placeholder hoặc tự nộp bài.\n\nDiễn tập Playwright **không ghi hình, chỉ đọc nghiệp vụ** trên app thật: chín\nmàn hình, sáuBR, bảy exports, scenario đã lưu có frozen pin cũ và sensor30events.\nDates trong UI/export phải khớp, KPI giữ oracle gốc; Irr10,36 thuộc correction\nhiện hành5d45, scenario/video gốc4b08 có context riêng. Journal62/head không đổi.\nTiming automation không phải24phút thuyết trình thực tế.\n\nBa lần lỗi harness được giữ: dropdown đang render, tên bộ lọc năng lượng riêng,\nvà nhập start vượt end làm control khôi phục ngày cũ. Sửa chờ/locator, nhập end\ntrước khi tiến range, thêm assertion exactUI/export dates; không đổi app/KPI oracle.\n`executed-rehearsal.cjs` là snapshot helper đã chạy; result/screenshots/downloads\nở `rehearsal/`. Logs lỗi không được dùng làm bằng chứng lượt cuối đạt.\n\nSourceZIP giữ local; CRC/path/mode/per-file SHA và đối chiếu source đạt. Giải nén\nriêng, Compose native khôngPG và CLI chạy từ extractedsource/networknone/image\nchuẩn bị sẵn đạt; không claim build lại dependencies hoặc fullAGC lần này.\nManifest baseHEAD là trước commit, per-file hashes mới là identity source chính xác.\nEvidence sinh ra và recursiveZIP bị loại khỏi source để tránh self-reference.\n\n`summary.json`/`manifest.json` giữ hashes và phạm vi; [chỉ mục gói](../../../../project-submit/README.md),\n[nguồn report](../../../submission/README.md). Điền danh tính, build lại/tạo\nmanifest, bổ sung video có audio và kiểm cuối trước nộp; nơi nộp/quyền truy cập\nchưa được chỉ định, agent không tự upload.\n''')
manifest=dict(files={p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()})
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print(json.dumps(dict(status='passed',files=len(manifest['files']),seconds=result['elapsed_seconds'])))
