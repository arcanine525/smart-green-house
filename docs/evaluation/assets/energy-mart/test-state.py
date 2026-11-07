"""Run pytest and attest the exact mounted source/tests before and after."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def state():
    result = {}
    for label, root in (('src', Path('/review-src')), ('tests', Path('/workspace/tests'))):
        for path in sorted(root.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.name != '.DS_Store':
                result[label+'/'+path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


before = state()
started = datetime.now(timezone.utc).isoformat()
print('TESTED_STATE', digest(before), 'files', len(before), flush=True)
args = [sys.executable, '-m', 'pytest'] + sys.argv[1:]
completed = subprocess.run(args, check=False)
after = state()
report = {'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
    'command': args, 'exit_code': completed.returncode, 'files': before,
    'state_sha256': digest(before), 'state_unchanged': before == after,
    'memory_peak': Path('/sys/fs/cgroup/memory.peak').read_text().strip(),
    'memory_max': Path('/sys/fs/cgroup/memory.max').read_text().strip(),
    'cpu_max': Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
    'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
Path(os.environ['EVIDENCE_REPORT']).write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
print('FINAL_TESTED_STATE', report['state_sha256'], 'unchanged', report['state_unchanged'], flush=True)
sys.exit(completed.returncode if before == after else 97)
