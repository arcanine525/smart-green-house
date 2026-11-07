"""One serial browser startup probe after restarting only the owned demo app."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--preparation', type=Path, required=True)
    parser.add_argument('--videos', type=Path, default=Path('project-submit/demo-videos/clickhouse'))
    parser.add_argument('--before-recording', action='store_true', help='Probe after six historical clips and a prepared partial replay, before recording sensor')
    parser.add_argument('--context', type=Path)
    args = parser.parse_args()
    folders = ['br-'+str(i).zfill(2) for i in range(1,7)]
    if args.before_recording:
        if args.context is None:
            raise ValueError('Explicit partial replay context required')
        context = json.loads(args.context.read_text())
        live = context['live_replay']
        if live['accepted_events'] != 10 or live['complete'] or Path(live['complete_result_host_path']).exists():
            raise ValueError('Untouched partial replay required before recording')
    else:
        folders.append('sensor-replay')
    for folder in folders:
        if json.loads((args.videos/folder/'result.json').read_text())['status'] != 'passed':
            raise ValueError('Finish prerequisite recordings before restarting the demo app')
    setup = json.loads(args.setup.read_text())
    runtime = json.loads((args.preparation / 'runtime.json').read_text())
    project = runtime['project']
    if (runtime['purpose'], runtime['backend'], runtime['database']) != (
            'isolated_walkthrough', 'clickhouse', 'greenhouse_repro'):
        raise ValueError('Isolated native walkthrough required')
    if not re.fullmatch(r'gh-ch-repro-[a-z0-9-]+', project) or project != setup['project'] + '-rebuilt':
        raise ValueError('Accepted disposable rebuild required')
    if shutil.disk_usage('.').free < 3 * 1024**3:
        raise ValueError('Need 3 GiB headroom for cold publication proof')
    container = project + '-ch-app-1'

    def inspect():
        return json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]

    before = inspect()
    if before['Config']['Labels']['com.docker.compose.project'] != project or before['Image'] != runtime['ui_image']:
        raise ValueError('App identity/image changed')
    output = args.preparation / 'cold-browser-startup'
    if output.exists():
        raise ValueError('Preserve the previous cold attempt')
    output.mkdir()
    shutil.copy2(args.preparation / 'runtime.json', output / 'runtime.json')
    started = time.monotonic()
    receipt = dict(status='running', backend='clickhouse', project=project, app=container,
        started_at=datetime.now(timezone.utc).isoformat(),
        mode='before_sensor_recording' if args.before_recording else 'after_all_recordings',
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        warm_script_sha256=hashlib.sha256(Path('tools/walkthrough/warm-ui.cjs').read_bytes()).hexdigest(), previous_app_started_at=before['State']['StartedAt'],
        no_parallel_browser=True, database_restarted_by_probe=False)
    try:
        with (Path(setup['root']) / 'runtime-stage.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            subprocess.run(['docker', 'restart', container], check=True)
            deadline = time.monotonic() + 90
            while inspect()['State']['Health']['Status'] != 'healthy':
                if time.monotonic() >= deadline:
                    raise TimeoutError('Owned app did not become healthy')
                time.sleep(.5)
            receipt['app_started_at'] = inspect()['State']['StartedAt']
            assert receipt['app_started_at'] != receipt['previous_app_started_at']
            with (output / 'browser.log').open('w') as log:
                subprocess.run(['node', 'tools/walkthrough/warm-ui.cjs', str(output)],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=660)
            receipt['status'] = 'passed'
            receipt['note'] = 'One fresh app process; cold process/proof probe, not a warm summary benchmark or cold disk claim'
    except Exception as error:
        receipt.update(status='failed', error=str(error))
        raise
    finally:
        receipt['restart_to_browser_seconds'] = time.monotonic() - started
        (output / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
