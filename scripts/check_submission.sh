#!/usr/bin/env bash
# Validate explicit local submission paths; never upload or alter artifacts.
set -euo pipefail
if [ "$#" -ne 2 ]; then
  printf 'Usage: %s GROUP_ID SUBMISSION_DIR\n' "$0" >&2
  exit 2
fi
group_id=$1
submission_dir=$2
if [[ ! $group_id =~ ^[A-Za-z0-9_-]+$ ]] || [[ ! -d $submission_dir ]]; then
  printf 'Invalid group ID or submission directory.\n' >&2
  exit 2
fi
for program in pdfinfo pdftotext ffprobe docker; do
  command -v "$program" >/dev/null || { printf 'Missing command: %s\n' "$program" >&2; exit 2; }
done
errors=0
for kind in technical_report slides; do
  path="$submission_dir/${group_id}_${kind}.pdf"
  if [[ ! -s $path ]] || ! pdfinfo "$path" >/dev/null 2>&1; then
    printf 'FAIL: missing or unreadable PDF %s\n' "$path"
    errors=$((errors + 1))
    continue
  fi
  # Require searchable, non-empty text; reject a document still labelled draft.
  pdf_text=$(pdftotext "$path" -)
  if [[ -z ${pdf_text//[[:space:]]/} ]] || grep -Eiq 'draft|[Bb]ản dự thảo' <<< "$pdf_text"; then
    printf 'FAIL: PDF has no text or is still labelled draft: %s\n' "$path"
    errors=$((errors + 1))
  else
    printf 'PASS: readable PDF %s\n' "$path"
  fi
done
video=''
for ext in mp4 webm; do
  if [[ -s "$submission_dir/${group_id}_video.$ext" ]]; then
    if [[ -n $video ]]; then
      printf 'FAIL: more than one candidate video; choose one.\n'
      errors=$((errors + 1))
    fi
    video="$submission_dir/${group_id}_video.$ext"
  fi
done
if [[ -z $video ]]; then
  printf 'FAIL: missing %s_video.mp4 or .webm\n' "$group_id"
  errors=$((errors + 1))
else
  duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$video" 2>/dev/null || true)
  video_stream=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "$video" 2>/dev/null || true)
  audio_stream=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_type -of csv=p=0 "$video" 2>/dev/null || true)
  if [[ ! $duration =~ ^[0-9]+([.][0-9]+)?$ ]] || [[ $video_stream != video ]] || [[ $audio_stream != audio ]] || \
     ! awk -v seconds="$duration" 'BEGIN {exit !(seconds >= 1200 && seconds <= 1800)}'; then
    printf 'FAIL: video must be readable, have video and audio, and last 20–30 minutes.\n'
    errors=$((errors + 1))
  else
    printf 'PASS: video streams and duration %s seconds\n' "$duration"
  fi
fi
if [[ ! -s compose.yaml || ! -s readme.md ]] || ! docker compose config --quiet; then
  printf 'FAIL: runnable project or Compose configuration missing.\n'
  errors=$((errors + 1))
else
  printf 'PASS: project manifest and Compose configuration available.\n'
fi
if (( errors )); then
  printf 'Submission check incomplete: %s issue(s).\n' "$errors"
  exit 1
fi
printf 'Automated checks passed. Manually review content, demo, group identity and submission access.\n'
