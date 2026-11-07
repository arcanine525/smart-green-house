"""No Internet or AGC payloads: tiny synthetic ZIPs exercise publication failures."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

from greenhouse.ingestion.inventory import load_verified_manifest, render_inventory
from greenhouse.ingestion.snapshot import ArchiveSpec, SnapshotError, create_snapshot, download_archive


@pytest.fixture(autouse=True)
def writable_test_cleanup(tmp_path):
    yield
    # Only pytest's synthetic temp tree: published directories are read-only.
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def synthetic_archive(tmp_path, member="Synthetic/Resources.csv", symlink=False):
    path = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(member)
        if symlink:
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "%Time ,Irr,Drain,,\n43831,10,2,,\n43832,30,12,,\n")
        archive.writestr("ReadMe.pdf", b"%PDF synthetic placeholder for archive tests only")
    data = path.read_bytes()
    spec = ArchiveSpec("synthetic-dataset", hashlib.sha256(data).hexdigest(), len(data),
                       "https://example.invalid/synthetic.zip", path.name, 1, 1)
    return path, spec


def test_local_manifest_preserves_source_header_hash_and_observations(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    snapshot, manifest, created = create_snapshot(archive, tmp_path / "raw", spec)
    assert created
    assert manifest["archive"] == {"sha256": spec.sha256, "size_bytes": archive.stat().st_size}
    assert (manifest["file_count"], manifest["csv_count"], manifest["pdf_count"], manifest["total_csv_rows"]) == (2, 1, 1, 2)
    data = next(f for f in manifest["files"] if f["type"] == "csv")
    assert data["csv"]["header_raw"] == "%Time ,Irr,Drain,,"
    assert data["csv"]["headers"] == ["%Time ", "Irr", "Drain", "", ""]
    assert data["csv"]["all_empty_column_indices"] == [3, 4]
    assert data["csv"]["time"]["min_raw"] == "43831"
    assert data["csv"]["time"]["positive_delta_median_days"] == "1"
    assert manifest["source"]["reference"] == "import/synthetic.zip"
    assert manifest["source"]["acquired_at_utc"].endswith("+00:00")
    assert str(tmp_path) not in json.dumps(manifest)
    assert (snapshot / "ReadMe.pdf").read_bytes().startswith(b"%PDF synthetic")
    assert (snapshot / "manifest.json").stat().st_mode & 0o222 == 0
    assert load_verified_manifest(snapshot) == manifest


def test_rerun_is_idempotent_without_rewriting_manifest_or_files(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    snapshot, manifest, _ = create_snapshot(archive, tmp_path / "raw", spec)
    before = {p.relative_to(snapshot): (p.stat().st_mtime_ns, p.read_bytes()) for p in snapshot.rglob("*") if p.is_file()}
    again, second, created = create_snapshot(archive, tmp_path / "raw", spec)
    assert not created and again == snapshot and second == manifest
    assert before == {p.relative_to(snapshot): (p.stat().st_mtime_ns, p.read_bytes()) for p in snapshot.rglob("*") if p.is_file()}


def test_checksum_mismatch_stops_before_creating_snapshot_and_keeps_archive(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    with pytest.raises(SnapshotError, match="expected.*actual"):
        create_snapshot(archive, tmp_path / "raw", replace(spec, sha256="0" * 64))
    assert archive.is_file() and not (tmp_path / "raw").exists()


def test_snapshot_cli_returns_failure_on_wrong_archive_without_publication(tmp_path, capsys):
    from greenhouse.cli import main
    archive, _ = synthetic_archive(tmp_path)
    result = main(["snapshot", "--archive", str(archive), "--raw-dir", str(tmp_path / "raw"),
                   "--fixtures", str(Path(__file__).parents[1] / "fixtures/agc_synthetic")])
    assert result == 1 and not (tmp_path / "raw").exists()
    message = capsys.readouterr().err
    assert "expected size=" in message and "actual size=" in message


@pytest.mark.parametrize("member,symlink", [
    ("../escaped.csv", False), ("/absolute.csv", False), ("C:/drive.csv", False),
    ("Synthetic\\bad.csv", False), ("Synthetic/link.csv", True),
])
def test_malicious_archive_rejected_before_extraction(tmp_path, member, symlink):
    archive, spec = synthetic_archive(tmp_path, member, symlink)
    with pytest.raises(SnapshotError):
        create_snapshot(archive, tmp_path / "raw", spec)
    assert not (tmp_path / "raw").exists()
    assert not (tmp_path / "escaped.csv").exists()


def test_expansion_limit_rejects_archive_before_publication(tmp_path, monkeypatch):
    archive, spec = synthetic_archive(tmp_path)
    monkeypatch.setattr("greenhouse.ingestion.snapshot.MAX_EXPANDED_BYTES", 10)
    with pytest.raises(SnapshotError, match="expanded-size"):
        create_snapshot(archive, tmp_path / "raw", spec)


@pytest.mark.parametrize("tamper_manifest", [False, True])
def test_existing_different_content_stops_even_if_manifest_is_also_changed(tmp_path, tamper_manifest):
    archive, spec = synthetic_archive(tmp_path)
    snapshot, manifest, _ = create_snapshot(archive, tmp_path / "raw", spec)
    path = snapshot / "Synthetic/Resources.csv"
    path.chmod(0o644)
    path.write_text(path.read_text().replace(",10,2", ",99,2"))
    if tamper_manifest:
        from greenhouse.ingestion.inventory import describe_file
        manifest["files"] = [describe_file(snapshot / record["path"], record["path"]) for record in manifest["files"]]
        manifest_path = snapshot / "manifest.json"
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    with pytest.raises(SnapshotError, match="content differs"):
        create_snapshot(archive, tmp_path / "raw", spec)
    assert path.read_bytes() == before  # No silent repair/overwrite.


def test_download_uses_get_and_atomically_caches_only_verified_archive(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    requests = []
    def opener(request, timeout):
        requests.append(request.get_method())
        return io.BytesIO(archive.read_bytes())
    path = download_archive(tmp_path / "raw", spec, opener)
    assert requests == ["GET"] and path.read_bytes() == archive.read_bytes()
    assert not list(path.parent.glob("*.part"))


def test_bad_download_keeps_partial_evidence_without_snapshot_or_verified_cache(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    with pytest.raises(SnapshotError, match="expected.*actual"):
        download_archive(tmp_path / "raw", spec, lambda *args, **kwargs: io.BytesIO(b"synthetic wrong response"))
    assert list((tmp_path / "raw/.downloads").glob("*.part"))
    assert not list((tmp_path / "raw/.downloads").glob("*.zip"))
    assert not (tmp_path / "raw" / spec.dataset).exists()


def test_inventory_is_deterministic_without_acquisition_clock(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    _, manifest, _ = create_snapshot(archive, tmp_path / "raw", spec)
    first = render_inventory(manifest)
    manifest["source"]["acquired_at_utc"] = "2000-01-01T00:00:00+00:00"
    assert render_inventory(manifest) == first
    assert "43831" in first and "%Time " in first and str(tmp_path) not in first


def test_corrupted_manifest_aggregate_is_rejected_instead_of_reused(tmp_path):
    archive, spec = synthetic_archive(tmp_path)
    snapshot, manifest, _ = create_snapshot(archive, tmp_path / "raw", spec)
    manifest["total_csv_rows"] = 999
    path = snapshot / "manifest.json"
    path.chmod(0o644)
    path.write_text(json.dumps(manifest))
    with pytest.raises(SnapshotError, match="aggregate counts"):
        create_snapshot(archive, tmp_path / "raw", spec)
