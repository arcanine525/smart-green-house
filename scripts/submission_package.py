#!/usr/bin/env python3
"""Build and verify the local assignment bundle, with an explicit pre-video gate."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'smart-green-house/'
GENERATED_EVIDENCE = 'docs/evaluation/assets/submission-package/'


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.STDOUT)


def receipt(path: Path, base: Path) -> dict:
    return dict(file=path.relative_to(base).as_posix(), bytes=path.stat().st_size,
                sha256=digest(path))


def allowed(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not PurePosixPath(name).is_absolute() and '..' not in parts and not (
        any(p in {'.git', '.aws', '.agents', '.codex', 'node_modules', '__pycache__'}
            for p in parts)
        or parts[0] in {'data', 'tmp', '.venv-ui'}
        or any(p.startswith('.env') and p != '.env.example' for p in parts)
        or name == 'TODO.MD'
        or name.startswith('project-submit/demo-videos/')
        or Path(name).suffix.lower() in {'.mp4', '.webm', '.mov', '.dump', '.sqlite'}
        or name.startswith(GENERATED_EVIDENCE)
        or (parts[0] == 'project-submit' and
            (name.endswith('_application.zip') or name.endswith('_delivery_manifest.json')))
    )


def videos(directory: Path) -> list[dict]:
    source = read_json(ROOT / 'docs/evaluation/assets/clickhouse-walkthrough/summary.json')
    assert source['backend'] == 'clickhouse'
    assert {v['br'] for v in source['videos']} == {f'{i:02d}' for i in range(1, 8)}
    base = directory / 'demo-videos/clickhouse'
    assert (base / 'index.html').is_file(), 'Missing local walkthrough player'
    results = []
    for video in source['videos']:
        folder = base / ('sensor-replay' if video['br'] == '07' else 'br-' + video['br'])
        path = folder / video['video']['file']
        assert digest(path) == video['video']['sha256'], f'Video changed: {path}'
        probe = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', str(path)))
        stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert (stream['width'], stream['height']) == (1920, 1080)
        duration = float(probe['format']['duration'])
        assert abs(duration - video['video']['duration_seconds']) < 0.1
        for download in video['downloads']:
            assert digest(folder / download['file']) == download['sha256'], 'Changed walkthrough export'
        results.append(dict(receipt(path, directory), br=video['br'], duration_seconds=duration,
                            width=1920, height=1080, downloads_verified=len(video['downloads']),
                            publication=video['publication'], kind='automated_walkthrough'))
    return results


def presentation(directory: Path, group: str) -> dict:
    paths = [directory / f'{group}_video.{ext}' for ext in ('mp4', 'webm')]
    present = [p for p in paths if p.is_file()]
    assert len(present) <= 1, 'Choose exactly one overall presentation video'
    if not present:
        return dict(status='pending_user_offline', required_minutes=[20, 30])
    path = present[0]
    probe = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-show_format',
                           '-of', 'json', str(path)))
    duration = float(probe['format']['duration'])
    assert {'video', 'audio'} <= {s['codec_type'] for s in probe['streams']}
    assert 1200 <= duration <= 1800, 'Overall presentation must last 20–30 minutes'
    return dict(receipt(path, directory), status='structurally_verified', duration_seconds=duration,
                manual_audio_content_review_required=True)


def build(directory: Path, group: str) -> None:
    assert directory == ROOT / 'project-submit', 'Build only in the requested project-submit directory'
    directory.mkdir(parents=True, exist_ok=True)
    names = run('git', 'ls-files', '-z').split('\0')
    files = {}
    for name in sorted(n for n in names if n and allowed(n)):
        path = ROOT / name
        assert path.is_file() and not path.is_symlink(), f'Missing/symlink source: {name}'
        mode = '755' if path.stat().st_mode & 0o111 else '644'
        files[name] = dict(bytes=path.stat().st_size, sha256=digest(path), mode=mode)
    required = {'compose.yaml', 'compose.clickhouse.yaml', '.env.example', 'readme.md',
                'scripts/submission_package.py', 'scripts/check_submission.sh',
                'src/greenhouse/__main__.py', 'requirements.lock',
                'requirements-ui.lock', 'docs/submission/group.json'}
    assert required <= files.keys(), 'Stage all intended source files before building the archive'
    changed = set(run('git', 'diff', '--name-only', 'HEAD', '--').splitlines())
    source = dict(kind='sha256_bound_source_snapshot',
                  source_base_head=run('git', 'rev-parse', 'HEAD').strip(),
                  source_bytes='working tree bytes of tracked/staged paths',
                  includes_uncommitted_source=bool(changed & files.keys()),
                  exclusion_policy='No personal env, git, datasets, dumps, media, generated package evidence or recursive archives',
                  files=files)
    target = directory / f'{group}_application.zip'
    temporary = target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        def put(name, data, mode=0o644):
            info = zipfile.ZipInfo(PREFIX + name, date_time=(2026, 11, 7, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        for name, meta in files.items():
            put(name, (ROOT / name).read_bytes(), int(meta['mode'], 8))
        put('source-manifest.json', json.dumps(source, indent=2).encode() + b'\n')
    temporary.replace(target)
    artifacts = [directory / f'{group}_{kind}' for kind in (
        'technical_report.pdf', 'slides.pdf', 'technical_report.docx', 'slides.pptx',
        'video_script.md', 'application.zip')]
    artifacts += [directory / 'README.md', directory / 'group.json']
    manifest = dict(kind='pre_video_delivery_bundle', group_id=group,
                    source_base_head=source['source_base_head'],
                    source_identity='Per-file hashes in ZIP source-manifest.json; base HEAD is not claimed as an exact clean source commit',
                    artifacts=[receipt(p, directory) for p in artifacts],
                    walkthroughs=videos(directory), overall_presentation=presentation(directory, group),
                    manual_remaining=['Fill three names/student IDs', 'Record and review overall presentation',
                                      'Confirm actual contributions and submission channel/access'])
    (directory / f'{group}_delivery_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Built {target}: {len(files)} source files; local video hashes verified.')


def check(directory: Path, group: str, pre_video: bool) -> None:
    manifest = read_json(directory / f'{group}_delivery_manifest.json')
    assert manifest['group_id'] == group
    for artifact in manifest['artifacts']:
        path = directory / artifact['file']
        assert path.stat().st_size == artifact['bytes'] and digest(path) == artifact['sha256'], f'Artifact changed: {path}; rebuild package'
    identity = read_json(directory / 'group.json')
    assert identity['group_id'] == group and len(identity['members']) == 3
    placeholders = any(re.search(r'\[(HỌ TÊN|MSSV)', m[k])
                       for m in identity['members'] for k in ('name', 'student_id'))
    for kind in ('technical_report', 'slides'):
        path = directory / f'{group}_{kind}.pdf'
        run('pdfinfo', str(path))
        text = ' '.join(run('pdftotext', str(path), '-').split())
        assert text and not re.search(r'draft|bản dự thảo', text, re.I)
        assert f'Group {group}' in text
        assert all(m['name'] in text and m['student_id'] in text for m in identity['members'])
        print(f'PASS: searchable PDF, group identity and placeholders policy: {path.name}')
    target = directory / f'{group}_application.zip'
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None, 'Archive CRC failed'
        source = json.loads(archive.read(PREFIX + 'source-manifest.json'))
        expected = {PREFIX + n for n in source['files']} | {PREFIX + 'source-manifest.json'}
        assert len(archive.namelist()) == len(expected) and set(archive.namelist()) == expected
        for name, meta in source['files'].items():
            assert allowed(name), f'Forbidden packaged path: {name}'
            entry = archive.getinfo(PREFIX + name)
            assert (entry.external_attr >> 16) & 0o170000 != 0o120000, 'Symlink in archive'
            data = archive.read(entry)
            assert len(data) == meta['bytes'] and hashlib.sha256(data).hexdigest() == meta['sha256']
            # Running from the full checkout: bind packaged bytes to reviewed source.
            current = ROOT / name
            assert current.is_file() and digest(current) == meta['sha256'], f'Source changed: {name}; rebuild package'
        print(f'PASS: application ZIP CRC, safe paths and {len(source["files"])} source hashes')
    current_videos = videos(directory)
    assert current_videos == manifest['walkthroughs'], 'Walkthrough inventory changed; rebuild package'
    print('PASS: seven native 1080p videos and 21 exports match accepted checksums')
    overall = presentation(directory, group)
    assert overall == manifest['overall_presentation'], 'Overall video changed; rebuild package'
    run('docker', 'compose', 'config', '--quiet')
    print('PASS: default Compose configuration')
    if pre_video:
        print('PRE_VIDEO_PASS: report/slides/application/walkthroughs verified; overall presentation and identity completion are handed to user.')
        print(f'Identity placeholders: {placeholders}; overall video: {overall["status"]}. This is not a complete four-product submission.')
    else:
        assert not placeholders, 'Fill member names/student IDs before final submission'
        assert overall['status'] == 'structurally_verified', 'Missing overall presentation; use --pre-video only for the earlier gate'
        print('FINAL_STRUCTURE_PASS: manually review all audio/content, contributions and submission access before submitting.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'check'])
    parser.add_argument('group')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--pre-video', action='store_true')
    args = parser.parse_args()
    assert re.fullmatch(r'[A-Za-z0-9_-]+', args.group), 'Invalid group ID'
    directory = args.directory.resolve()
    if args.action == 'build':
        build(directory, args.group)
    else:
        check(directory, args.group, args.pre_video)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
