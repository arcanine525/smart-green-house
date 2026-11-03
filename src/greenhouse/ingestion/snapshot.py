"""Freeze a checksum-locked archive without writing database or source inputs."""

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import urllib.request
import zipfile

from greenhouse.ingestion.inventory import describe_file, summarize_files


@dataclass(frozen=True)
class ArchiveSpec:
    dataset: str
    sha256: str
    size_bytes: int
    url: str
    filename: str
    csv_count: int
    pdf_count: int


AGC = ArchiveSpec(
    "agc-2nd-2019",
    "c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68",
    12_793_984,
    "https://www.kaggle.com/api/v1/datasets/download/piantic/autonomous-greenhouse-challengeagc-2nd-2019",
    "autonomous-greenhouse-challengeagc-2nd-2019.zip", 43, 2,
)
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 128


class SnapshotError(ValueError):
    pass


def digest_file(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_archive(path, spec=AGC):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("Archive must be a regular file, not a symlink")
    actual_size, actual_sha = path.stat().st_size, digest_file(path)
    if (actual_size, actual_sha) != (spec.size_bytes, spec.sha256):
        raise SnapshotError(
            f"Archive mismatch: expected size={spec.size_bytes}, sha256={spec.sha256}; "
            f"actual size={actual_size}, sha256={actual_sha}. Evidence preserved."
        )


def download_archive(raw_root, spec=AGC, opener=None):
    """GET with redirects; preserve partial/mismatched bytes for diagnosis."""
    cache = Path(raw_root) / ".downloads"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{spec.sha256}.zip"
    if target.exists() or target.is_symlink():
        verify_archive(target, spec)
        return target
    descriptor, filename = tempfile.mkstemp(prefix="download-", suffix=".part", dir=cache)
    partial = Path(filename)
    request = urllib.request.Request(spec.url, headers={"User-Agent": "SmartGreenhouse-snapshot/1.0"}, method="GET")
    # A failed .part is deliberately retained; it is never a published snapshot.
    with os.fdopen(descriptor, "wb") as output:
        with (opener or urllib.request.urlopen)(request, timeout=60) as response:
            received = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                if received > spec.size_bytes + 1024 * 1024:
                    break  # Bound the response; verify reports actual partial size/hash below.
        output.flush()
        os.fsync(output.fileno())
    verify_archive(partial, spec)
    partial.rename(target)
    target.chmod(0o444)
    return target


def archive_members(archive, spec=AGC):
    members = archive.infolist()
    if len(members) > MAX_ENTRIES or sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
        raise SnapshotError("Archive exceeds entry/expanded-size limit")
    seen, files = set(), []
    for member in members:
        name = member.filename
        parts = name.rstrip("/").split("/")
        mode = member.external_attr >> 16
        if (not name or name != member.orig_filename or "\\" in name or ":" in name or "\x00" in name
                or PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") for p in parts)):
            raise SnapshotError("Unsafe archive path")
        if name.casefold().rstrip("/") in seen:
            raise SnapshotError("Duplicate archive path")
        seen.add(name.casefold().rstrip("/"))
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or stat.S_ISLNK(mode):
            raise SnapshotError("Archive symlink/special file rejected")
        if member.flag_bits & 1 or member.file_size > MAX_FILE_BYTES:
            raise SnapshotError("Encrypted/oversized archive member rejected")
        if not member.is_dir():
            if Path(name).suffix.lower() not in (".csv", ".pdf"):
                raise SnapshotError("Unexpected archive file type")
            files.append(member)
    counts = {suffix: sum(Path(m.filename).suffix.lower() == suffix for m in files) for suffix in (".csv", ".pdf")}
    if counts != {".csv": spec.csv_count, ".pdf": spec.pdf_count}:
        raise SnapshotError(f"Archive layout count mismatch: {counts}")
    paths = {m.filename for m in files}
    if any(parent.as_posix() in paths for name in paths for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
        raise SnapshotError("File/directory prefix collision in archive")
    if spec == AGC:
        teams = ("AICU", "Automatoes", "Digilog", "IUACAAS", "Reference", "TheAutomators")
        kinds = ("CropParameters", "GreenhouseClimate", "GrodanSens", "LabAnalysis", "Production", "Resources", "TomQuality")
        expected = {f"{team}/{kind}.csv" for team in teams for kind in kinds} | {"Weather/Weather.csv", "ReadMe.pdf", "Economics.pdf"}
        if paths != expected:
            raise SnapshotError("Archive paths differ from locked AGC layout")
    return sorted(files, key=lambda m: m.filename)


def validate_snapshot(snapshot, archive, members, spec=AGC):
    """Recheck disk and manifest against the checksum-locked ZIP, not trust metadata."""
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise SnapshotError("Existing snapshot is not a regular directory")
    paths = list(snapshot.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise SnapshotError("Symlink in existing snapshot")
    expected = {m.filename for m in members} | {"manifest.json"}
    if {p.relative_to(snapshot).as_posix() for p in paths if p.is_file()} != expected:
        raise SnapshotError("Existing snapshot file set differs; refusing overwrite")
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text())
        if (manifest["manifest_version"] != 1 or manifest["dataset"] != spec.dataset
                or manifest["snapshot_id"] != f"{spec.dataset}-{spec.sha256}"
                or manifest["archive"]["sha256"] != spec.sha256
                or manifest["archive"]["size_bytes"] != spec.size_bytes):
            raise SnapshotError("Existing manifest identity mismatch")
        records = {entry["path"]: entry for entry in manifest["files"]}
        if set(records) != {m.filename for m in members} or len(records) != len(manifest["files"]):
            raise SnapshotError("Existing manifest file list differs")
        for member in members:
            path = snapshot / member.filename
            with archive.open(member) as source:
                locked_digest = hashlib.file_digest(source, "sha256").hexdigest()
            actual = describe_file(path, member.filename)
            if actual["sha256"] != locked_digest or actual != records[member.filename]:
                raise SnapshotError(f"Existing snapshot content differs: {member.filename}; refusing overwrite")
        if any(manifest.get(key) != value for key, value in summarize_files(list(records.values())).items()):
            raise SnapshotError("Existing manifest aggregate counts differ")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SnapshotError("Invalid existing snapshot manifest") from error
    return manifest


def create_snapshot(archive_path, raw_root, spec=AGC, source_method="local"):
    archive_path, raw_root = Path(archive_path), Path(raw_root)
    verify_archive(archive_path, spec)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive_members(archive, spec)  # Validate every name before extracting anything.
        parent = raw_root / spec.dataset
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / spec.sha256
        # Linux container advisory lock serializes all publishers of this dataset.
        with (parent / ".snapshot.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.exists() or destination.is_symlink():
                manifest = validate_snapshot(destination, archive, members, spec)
                return destination, manifest, False
            pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
            for member in members:
                target = pending / member.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > member.file_size or copied > MAX_FILE_BYTES:
                            raise SnapshotError("Expanded member exceeds declared size")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if copied != member.file_size:
                    raise SnapshotError("Expanded member size mismatch")
            files = [describe_file(pending / m.filename, m.filename) for m in members]
            manifest = {
                "manifest_version": 1, "snapshot_id": f"{spec.dataset}-{spec.sha256}",
                "dataset": spec.dataset,
                "source": {"method": source_method, "reference": spec.url if source_method == "download" else f"import/{archive_path.name}",
                           "acquired_at_utc": datetime.now(timezone.utc).isoformat(), "url": spec.url},
                "archive": {"sha256": spec.sha256, "size_bytes": spec.size_bytes},
                **summarize_files(files),
                "files": files,
            }
            with (pending / "manifest.json").open("x") as output:
                json.dump(manifest, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            for path in pending.rglob("*"):
                path.chmod(0o555 if path.is_dir() else 0o444)
            pending.chmod(0o555)
            pending.rename(destination)  # Same filesystem; incomplete directories are never public.
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return destination, manifest, True
