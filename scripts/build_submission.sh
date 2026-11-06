#!/usr/bin/env bash
# Rebuild draft PDFs from tracked Markdown and Mermaid sources.
set -euo pipefail
cd "$(dirname "$0")/.."
for program in mmdc pandoc soffice; do
  command -v "$program" >/dev/null || { printf 'Missing command: %s\n' "$program" >&2; exit 1; }
done
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/greenhouse-submission.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
mmdc -i docs/submission/figures/architecture.mmd -o docs/submission/figures/architecture.png -b white -w 1400
mmdc -i docs/submission/figures/architecture-slide.mmd -o docs/submission/figures/architecture-slide.png -b white -w 1600
pandoc docs/submission/report.md --resource-path=docs/submission -o "$build_dir/report-draft.docx"
pandoc docs/submission/slides.md --resource-path=docs/submission -t pptx --slide-level=2 -o "$build_dir/slides-draft.pptx"
soffice "-env:UserInstallation=file://$build_dir/lo-profile" --headless --convert-to pdf \
  --outdir docs/submission "$build_dir/report-draft.docx" "$build_dir/slides-draft.pptx"
printf 'Draft PDFs: docs/submission/report-draft.pdf, docs/submission/slides-draft.pdf\n'
