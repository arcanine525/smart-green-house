"""Validate the tracked synthetic source fixtures without generating new data."""

import csv
import hashlib
import json
from pathlib import Path


def verify_fixtures(root):
    root = Path(root)
    catalog = json.loads((root / "catalog.json").read_text())
    if catalog.get("data_kind") != "synthetic":
        raise ValueError("Fixture catalog must be explicitly synthetic")
    expected = {f"BR-{br:02d}/AC-{ac:02d}" for br in range(1, 7) for ac in range(1, 12 if br == 6 else 11)}
    if set(catalog["cases"]) != expected:
        raise ValueError("Fixture AC coverage is incomplete")
    files = set()
    for key, case in catalog["cases"].items():
        if not case.get("files") or "expected" not in case or case.get("data_kind") != "synthetic":
            raise ValueError(f"Missing fixture data/oracle: {key}")
        files.update(case["files"])
    files.update(catalog["errors"])
    if any(error.get("data_kind") != "synthetic" or not error.get("expected") for error in catalog["errors"].values()):
        raise ValueError("Error fixtures must also be labelled synthetic with an expected outcome")
    if files != set(catalog["file_checksums"]):
        raise ValueError("Fixture file catalog differs from checksum list")
    if {p.relative_to(root).as_posix() for p in root.rglob("*.csv")} != files:
        raise ValueError("Uncatalogued/missing fixture CSV")
    for relative, expected_sha in catalog["file_checksums"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Unsafe fixture path")
        path = root / relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f"Fixture checksum differs: {relative}")
        if not relative.startswith("errors/"):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                if header != catalog["source_headers"][path.name]:
                    raise ValueError(f"Fixture header differs: {relative}")
                for row in reader:
                    if len(row) != len(header):
                        raise ValueError(f"Fixture row width differs: {relative}")
    return {"data_kind": "synthetic", "cases": len(catalog["cases"]), "files": len(files),
            "error_files": len(catalog["errors"])}
