"""Golden fixture data/oracles are separate from future KPI implementations."""

import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil
import pytest

from greenhouse.ingestion.fixtures import verify_fixtures

ROOT = Path(__file__).parents[1] / "fixtures/agc_synthetic"


def test_fixture_catalog_covers_all_ac_ids_and_source_shapes():
    result = verify_fixtures(ROOT)
    assert result == {"data_kind": "synthetic", "cases": 61, "files": 82, "error_files": 6}


def test_source_fixture_bytes_preserve_crlf_and_declared_bom_variant():
    catalog = json.loads((ROOT / "catalog.json").read_text())
    bom_files = set(catalog["byte_format"]["utf8_bom_files"])
    assert catalog["byte_format"]["line_terminator"] == "CRLF"
    assert bom_files and any(name.endswith("LabAnalysis.csv") for name in bom_files)
    for relative in catalog["file_checksums"]:
        path = ROOT / relative
        data = path.read_bytes()
        assert data.endswith(b"\r\n"), relative
        without_terminators = data.replace(b"\r\n", b"")
        assert b"\n" not in without_terminators and b"\r" not in without_terminators, relative
        assert data.startswith(b"\xef\xbb\xbf") == (relative in bom_files), relative
        if not relative.startswith("errors/"):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                assert next(csv.reader(handle)) == catalog["source_headers"][path.name]


@pytest.mark.parametrize("change", ["label", "case", "extra_csv"])
def test_fixture_verifier_detects_missing_labels_cases_or_uncatalogued_data(tmp_path, change):
    root = tmp_path / "synthetic"
    shutil.copytree(ROOT, root)
    path = root / "catalog.json"
    catalog = json.loads(path.read_text())
    if change == "label":
        next(iter(catalog["errors"].values()))["data_kind"] = "historical"
    elif change == "case":
        del catalog["cases"]["BR-01/AC-01"]
    else:
        (root / "unlabelled.csv").write_text("unexpected,data\n")
    path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError):
        verify_fixtures(root)


def rows_for(br, ac, kind):
    catalog = json.loads((ROOT / "catalog.json").read_text())
    case = catalog["cases"][f"BR-{br:02d}/AC-{ac:02d}"]
    path = next(p for p in case["files"] if p.endswith(kind + ".csv"))
    with (ROOT / path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle)), case["expected"]


def test_water_and_harvest_golden_inputs_match_literal_oracles():
    rows, expected = rows_for(2, 1, "Resources")
    assert sum(Decimal(row["Irr"]) for row in rows) == Decimal(str(expected["irrigation"])) == 40
    assert sum(Decimal(row["Drain"]) for row in rows) == Decimal(str(expected["drain"])) == 14
    rows, expected = rows_for(5, 1, "Production")
    assert sum(Decimal(row["ProdA"]) for row in rows) == Decimal(str(expected["a"])) == 3
    assert sum(Decimal(row["ProdB"]) for row in rows) == Decimal(str(expected["b"])) == Decimal("0.2")


def test_climate_and_drain_examples_are_the_br_vectors_not_dataset_rows():
    rows, expected = rows_for(1, 1, "GreenhouseClimate")
    assert [row["Tair"] for row in rows] == ["19", "21", "26", "26", "", "24"]
    assert expected["episode_minutes"] == [5, 10]
    rows, expected = rows_for(3, 1, "GreenhouseClimate")
    assert [row["EC_drain_PC"] for row in rows] == ["3", "6", "6", "", "3", "6"]
    assert [row["pH_drain_PC"] for row in rows] == ["6", "6", "7", "6", "0", "6"]
    assert expected["episode_minutes"] == [10, 5]


def test_energy_and_cost_golden_inputs_keep_explicit_synthetic_basis():
    rows, expected = rows_for(4, 1, "Resources")
    heat = sum(Decimal(row["Heat_cons"]) for row in rows) / Decimal("3.6") * 100
    electricity = sum(Decimal(row["ElecHigh"]) + Decimal(row["ElecLow"]) for row in rows) * 100
    harvest, _ = rows_for(4, 1, "Production")
    assert heat + electricity == 500 == expected["energy_kwh"]
    assert sum(Decimal(row["ProdA"]) for row in harvest) * 50 == 100
    rows, expected = rows_for(6, 1, "Resources")
    row = rows[0]
    baseline = Decimal(row["Heat_cons"]) * Decimal("0.0083") + Decimal(row["ElecHigh"]) * Decimal("0.08") + Decimal(row["ElecLow"]) * Decimal("0.04")
    assert baseline == Decimal("1.83") == Decimal(str(expected["total"]))
    catalog = json.loads((ROOT / "catalog.json").read_text())
    assert "phụ thuộc T03" in catalog["agc_basis_timezone_tariff_validation"]
