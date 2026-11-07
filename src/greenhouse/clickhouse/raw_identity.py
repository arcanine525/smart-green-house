"""Verify mounted snapshot identity as well as the selected source file entry."""
import json
from pathlib import Path


def source_path(raw,snapshot_id,source):
    raw=Path(raw)
    relative=Path(source['source_file'])
    target=raw/relative
    if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative) or any(p.is_symlink() for p in [target,*target.parents]):
        raise ValueError('Unsafe raw lineage path')
    manifest=json.loads((raw/'manifest.json').read_text())
    matches=[e for e in manifest.get('files',[]) if e['path']==source['source_file']]
    if (manifest.get('snapshot_id')!=snapshot_id or manifest.get('archive',{}).get('sha256')!=source['snapshot_sha']
            or len(matches)!=1 or matches[0]['sha256']!=source['file_checksum']):
        raise ValueError('Raw snapshot identity/checksum mismatch')
    return target
