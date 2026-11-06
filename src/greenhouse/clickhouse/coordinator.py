"""Local POSIX lock. All supported writers must mount the same directory."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def writer_lock(root):
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError('Existing absolute coordinator directory required')
    descriptor = os.open(root/'writer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
