#!/usr/bin/env bash
# Export the current project report/deck, editable files and recording guide locally.
set -euo pipefail
cd "$(dirname "$0")/.."
for program in python3 mmdc pandoc soffice pdfinfo; do
  command -v "$program" >/dev/null || { printf 'Missing command: %s\n' "$program" >&2; exit 1; }
done
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/greenhouse-submission.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
# Identity has one editable source. Placeholder names/IDs are intentional.
python3 - "$build_dir" <<'PY'
from pathlib import Path
import json,re,sys
root=Path(sys.argv[1]);group=json.loads(Path('docs/submission/group.json').read_text())
assert re.fullmatch(r'[A-Za-z0-9_-]+',group['group_id'])
assert len(group['members'])==3 and {m['role'] for m in group['members']}=={'A','B','C'}
assert sorted(b for m in group['members'] for b in m['business_requirements'])==[f'BR-{i:02d}' for i in range(1,7)]
assert all(m['name'].strip() and m['student_id'].strip() for m in group['members'])
(root/'group-id').write_text(group['group_id'])
(root/'identity.json').write_text(json.dumps({'author':[m['role']+': '+m['name']+' · MSSV '+m['student_id'] for m in group['members']]},ensure_ascii=False))
PY
group_id=$(cat "$build_dir/group-id")
submission_dir=project-submit
mkdir -p "$submission_dir"
mmdc -i docs/submission/figures/architecture.mmd -o docs/submission/figures/architecture.png -b white -w 1400
mmdc -i docs/submission/figures/architecture-slide.mmd -o docs/submission/figures/architecture-slide.png -b white -w 1600
pandoc docs/submission/report.md --metadata-file "$build_dir/identity.json" \
  --resource-path=docs/submission \
  -o "$build_dir/${group_id}_technical_report.docx"
pandoc docs/submission/slides.md --metadata-file "$build_dir/identity.json" \
  --resource-path=docs/submission -t pptx --slide-level=2 -o "$build_dir/${group_id}_slides.pptx"
# Apply A4 directly; preserve the remaining OpenXML bytes and namespaces.
python3 - "$build_dir/${group_id}_technical_report.docx" <<'PY'
from pathlib import Path
import sys,zipfile,re
p=Path(sys.argv[1])
with zipfile.ZipFile(p) as z:files={n:z.read(n) for n in z.namelist()}
xml=files['word/document.xml'].decode('utf-8')
section=('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
 '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>')
if '<w:sectPr' in xml:
 xml=re.sub(r'<w:sectPr\b[^>]*>.*?</w:sectPr>|<w:sectPr\s*/>',lambda _:section,xml,flags=re.S)
else:xml=xml.replace('</w:body>',section+'</w:body>')
assert '<w:pgSz w:w="11906" w:h="16838"/>' in xml
files['word/document.xml']=xml.encode('utf-8')
with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
 for name,value in files.items():z.writestr(name,value)
PY
mkdir "$build_dir/pdf"
soffice "-env:UserInstallation=file://$build_dir/lo-profile" --headless --convert-to pdf \
  --outdir "$build_dir/pdf" "$build_dir/${group_id}_technical_report.docx" "$build_dir/${group_id}_slides.pptx"
for kind in technical_report slides; do
  pdfinfo "$build_dir/pdf/${group_id}_${kind}.pdf" >/dev/null
done
cp "$build_dir/pdf/${group_id}_technical_report.pdf" "$build_dir/pdf/${group_id}_slides.pdf" "$submission_dir/"
cp "$build_dir/${group_id}_technical_report.docx" "$build_dir/${group_id}_slides.pptx" "$submission_dir/"
cp docs/submission/video-script.md "$submission_dir/${group_id}_video_script.md"
cp docs/submission/group.json "$submission_dir/group.json"
printf 'Built: %s/%s_technical_report.pdf, %s_slides.pdf and editable sources.\n' "$submission_dir" "$group_id" "$group_id"
printf 'Overall presentation video is recorded offline by the user.\n'
