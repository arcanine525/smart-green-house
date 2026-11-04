"""Synthetic CSV faults exercise source lineage and ingestion classifications."""

from datetime import datetime
from decimal import Decimal
import csv
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from greenhouse.cli import main
from greenhouse.ingestion.parser import HEADERS, Source, SourceParser
from greenhouse.ingestion.run import ingest_file, ingest_snapshot

FIXTURES = Path(__file__).parents[1] / "fixtures/agc_synthetic"


def source(path, relative="Synthetic/Resources.csv"):
    return Source("synthetic-snapshot", "a" * 64, relative,
                  hashlib.sha256(path.read_bytes()).hexdigest(), "synthetic-run")


def synth(tmp_path, kind="Resources", rows=None, headers=None, bom=False):
    path = tmp_path / f"{kind}.csv"
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(headers or HEADERS[kind])
        writer.writerows(rows or [["43831", *("1" for _ in HEADERS[kind][1:])]])
    return path


def records(folder, category, relative="Synthetic/Resources.csv"):
    with gzip.open(folder / category / (relative + ".jsonl.gz"), "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def check_counts(result):
    assert result["read"] == sum(result[key] for key in ("accepted", "quarantine", "duplicate", "conflict"))


@pytest.mark.parametrize("case,counts,status,code", [
    ("invalid_format", (1,0,1,0,0), "loaded_with_quarantine", "invalid_numeric"),
    ("missing_columns", (1,0,1,0,0), "failed", "schema_columns"),
    ("missing_values", (1,1,0,0,0), "loaded", None),
    ("duplicate", (2,1,0,1,0), "loaded", None),
    ("conflict", (2,0,0,0,2), "loaded_with_quarantine", "conflicting_key"),
    ("broken_csv", (1,0,1,0,0), "failed", "csv_lexical_error"),
])
def test_catalogued_error_fixtures_preserve_file_line_raw_and_balanced_counts(tmp_path, case, counts, status, code):
    path = FIXTURES / "errors" / case / "Synthetic/Resources.csv"
    output = tmp_path / "run"
    result = ingest_file(path, source(path), output)
    assert tuple(result[key] for key in ("read", "accepted", "quarantine", "duplicate", "conflict")) == counts
    assert result["status"] == status
    check_counts(result)
    emitted = sum((records(output, category) for category in ("accepted", "quarantine", "duplicate")), [])
    assert len(emitted) == result["read"]
    for row in emitted:
        assert row["source_file"] == "Synthetic/Resources.csv" and row["line_number"] >= 2
        assert row["run_id"] == "synthetic-run" and row["snapshot_sha"] == "a"*64
        assert row["file_checksum"] == source(path).file_checksum
        assert row["raw_hash"] == hashlib.sha256(row["raw_text"].encode()).hexdigest()
        assert row["raw_text"].endswith("\r\n")
    if code:
        assert any(code in [issue["code"] for issue in row["issues"]] for row in emitted)
    if case == "missing_values":
        assert emitted[0]["values"]["Irr"] is None
        assert emitted[0]["null_reasons"] == {"Irr":"empty", "Drain":"empty"}
        assert result["null_counts"] == {"Irr":1, "Drain":1}
    if case == "conflict":
        assert not records(output,"accepted")
        assert {row["line_number"] for row in emitted} == {2,3}


@pytest.mark.parametrize("token,reason", [("", "empty"),("NaN", "nan_token"),("NA", "na_token"),("N/A", "n_a_token")])
def test_null_tokens_are_retained_without_becoming_zero(tmp_path, token, reason):
    path=synth(tmp_path, rows=[["43831","0","0","0","0",token,"0"]])
    row=next(SourceParser(path,source(path)).records())
    assert row["raw_tokens"][5] == token
    assert row["values"]["Irr"] is None and row["null_reasons"]["Irr"] == reason
    assert row["values"]["Drain"] == 0 and not row["issues"]


@pytest.mark.parametrize("token", ["bad", "Inf", "-Infinity"])
def test_non_numeric_and_infinite_quantities_are_errors_not_zero(tmp_path,token):
    path=synth(tmp_path,rows=[["43831","0","0","0","0",token,"0"]])
    row=next(SourceParser(path,source(path)).records())
    assert row["values"]["Irr"] is None and row["raw_tokens"][5] == token
    assert {issue["code"] for issue in row["issues"]} == {"invalid_numeric"}


def test_header_normalization_supports_order_and_detects_collisions(tmp_path):
    headers=["  Drain\t", " %Time ", "Heat_cons", "ElecHigh", "ElecLow", "CO2_cons", "Irr"]
    path=synth(tmp_path,headers=headers,rows=[["2","43831","0","0","0","0","10"]])
    row=next(SourceParser(path,source(path)).records())
    assert row["values"]["Drain"]==2 and row["values"]["Irr"]==10 and not row["issues"]
    path=synth(tmp_path,headers=[*HEADERS["Resources"]," Irr\t"],rows=[["43831",*("0" for _ in range(7))]])
    parser=SourceParser(path,source(path))
    assert "header_collision" in {i["code"] for i in parser.file_issues}
    assert not next(parser.records())["values"]


@pytest.mark.parametrize("width",[6,8])
def test_wrong_row_width_never_shifts_or_truncates_values(tmp_path,width):
    path=synth(tmp_path,rows=[["43831",*("0" for _ in range(width-1))]])
    row=next(SourceParser(path,source(path)).records())
    assert not row["values"] and row["issues"][0]["code"]=="row_width"
    assert len(row["raw_tokens"])==width


def test_iuacaas_adapter_preflights_entire_tail_before_any_acceptance(tmp_path):
    header=HEADERS["Resources"]+[""]*5
    path=synth(tmp_path,headers=header,rows=[["43831",*("0" for _ in range(6)),*([""]*5)]])
    parser=SourceParser(path,source(path,"IUACAAS/Resources.csv"))
    row=next(parser.records())
    assert not row["issues"] and len(row["raw_tokens"])==12 and len(row["values"])==7
    assert parser.adapter_id=="agc-iuacaas-resources-empty-tail" and parser.adapter_version==1
    # A later nonempty column invalidates the whole adapter, including earlier rows.
    with path.open("a",newline="") as handle:
        csv.writer(handle,lineterminator="\r\n").writerow(["43832",*("0" for _ in range(6)),"unexpected",*([""]*4)])
    result=ingest_file(path,source(path,"IUACAAS/Resources.csv"),tmp_path/"run")
    assert result["status"]=="failed" and result["accepted"]==0 and result["quarantine"]==2
    assert result["file_issues"][0]["line"]==3
    check_counts(result)


@pytest.mark.parametrize("change",["name","width"])
def test_iuacaas_signature_is_not_a_generic_truncate_adapter(tmp_path,change):
    header=HEADERS["Resources"]+[""]*5
    if change=="name": header[-1]="new-column"
    else: header=header[:-1]
    path=synth(tmp_path,headers=header,rows=[["43831",*("0" for _ in header[1:])]])
    parser=SourceParser(path,source(path,"IUACAAS/Resources.csv"))
    assert parser.file_issues and not next(parser.records())["values"]


def test_bom_crlf_and_multiline_physical_lineage(tmp_path):
    path=FIXTURES/"br_03/ac_08/Synthetic/LabAnalysis.csv"
    parser=SourceParser(path,source(path,"Synthetic/LabAnalysis.csv"))
    assert parser.bom
    assert next(parser.records())["line_number"]==2
    path=synth(tmp_path,rows=[["43831","0","0","0","0","10\r\n","2"],["43832","0","0","0","0","3","1"]],bom=True)
    parser=SourceParser(path,source(path)); rows=list(parser.records())
    assert parser.bom and rows[0]["line_number"]==2 and rows[0]["line_end"]==3
    assert rows[1]["line_number"]==4 and rows[0]["values"]["Irr"]==10


@pytest.mark.parametrize("serial,slot,residual,error",[
    ("43815.00347",datetime(2019,12,16,0,5),Decimal("0.19200"),None),
    ("43815.000005",datetime(2019,12,16),Decimal("0.432000"),None),
    ("43815.000006",None,Decimal("0.518400"),"off_grid_time"),
    ("43981.00000",datetime(2020,5,30),Decimal(0),None),
    ("43980.999999",datetime(2020,5,30),Decimal("0.086400"),None),
])
def test_naive_grid_boundary_and_residual_policy(tmp_path,serial,slot,residual,error):
    path=synth(tmp_path,"GreenhouseClimate",rows=[[serial,*("1" for _ in HEADERS["GreenhouseClimate"][1:])]])
    row=next(SourceParser(path,source(path,"Synthetic/GreenhouseClimate.csv")).records())
    assert row["slot_start"]==slot and row["slot_residual_seconds"]==residual
    assert row["source_timestamp"].tzinfo is None
    assert row["values"]["time"]==Decimal(serial)
    assert row["is_close_boundary"]==(slot==datetime(2020,5,30))
    assert [issue["code"] for issue in row["issues"]]==([error] if error else [])


@pytest.mark.parametrize("serial",["NaN","nonsense","1e100","1e10000000","43831.5"])
def test_invalid_missing_out_of_range_and_fractional_daily_time(tmp_path,serial):
    path=synth(tmp_path,rows=[[serial,*("0" for _ in range(6))]])
    row=next(SourceParser(path,source(path)).records())
    assert row["issues"] and row["logical_key"] is None


def test_out_of_cycle_production_keeps_year_and_versioned_quarantine(tmp_path):
    # Entirely synthetic, deliberately unrelated to the real Reference record.
    path=synth(tmp_path,"Production",rows=[["43000",*("1" for _ in HEADERS["Production"][1:])]])
    row=next(SourceParser(path,source(path,"Reference/Production.csv")).records())
    assert row["values"]["time"]==43000
    assert row["issues"]==[{"code":"out_of_cycle_unresolved","policy_version":"agc-production-window-v1"}]
    result=ingest_file(path,source(path,"Reference/Production.csv"),tmp_path/"run",1)
    assert result["accepted"]==0 and result["quarantine"]==1 and not result["complete_for_ingestion"]


def test_slot_collision_and_parse_invalid_conflicts_withhold_first_winner(tmp_path):
    width=len(HEADERS["GreenhouseClimate"])
    path=synth(tmp_path,"GreenhouseClimate",rows=[["43815",*("1" for _ in range(width-1))],["43815.000001",*("1" for _ in range(width-1))]])
    result=ingest_file(path,source(path,"Synthetic/GreenhouseClimate.csv"),tmp_path/"climate")
    assert result["conflict"]==2 and result["accepted"]==0
    path=synth(tmp_path,rows=[["43831","0","0","0","0","10","2"],["43831","0","0","0","0","bad","2"]])
    result=ingest_file(path,source(path),tmp_path/"resource")
    assert result["conflict"]==2 and result["accepted"]==0


def test_manifest_counts_deferred_tomquality_and_source_identity_across_runs(tmp_path):
    root=tmp_path/"snapshot"; root.mkdir()
    path=synth(root,rows=[["43831","0","0","0","0","10","2"]])
    tom=root/"Reference"/"TomQuality.csv";tom.parent.mkdir()
    tom.write_bytes(b'%time,Weight\tDMC_fruit\r\n43831,123,4\r\n')
    def entry(p,rows):
        return {"path":p.relative_to(root).as_posix(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"csv":{"data_rows":rows}}
    manifest={"snapshot_id":"synthetic","archive":{"sha256":"b"*64},"files":[entry(path,1),entry(tom,1)]}
    folder,result=ingest_snapshot(root,manifest,tmp_path/"out", "first")
    assert result["status"]=="completed" and result["counts"]=={"read":1,"accepted":1,"quarantine":0,"duplicate":0,"conflict":0}
    assert result["file_status_counts"]=={"deferred":1,"loaded":1} and result["deferred_rows"]==1
    deferred=next(f for f in result["files"] if f["status"]=="deferred")
    assert deferred["source_file"]=="Reference/TomQuality.csv" and not deferred["complete_for_ingestion"]
    assert json.loads((folder/"manifest.json").read_text())["status"]=="completed"
    second,_=ingest_snapshot(root,manifest,tmp_path/"out","second")
    first_row=records(folder,"accepted","Resources.csv")[0]
    second_row=records(second,"accepted","Resources.csv")[0]
    assert first_row["source_record_id"]==second_row["source_record_id"] and first_row["run_id"]!=second_row["run_id"]
    with pytest.raises(FileExistsError): ingest_snapshot(root,manifest,tmp_path/"out","first")
    with pytest.raises(ValueError): ingest_snapshot(root,manifest,root/"output")


def test_expected_count_mismatch_marks_file_failed(tmp_path):
    path=synth(tmp_path)
    result=ingest_file(path,source(path),tmp_path/"run",expected_rows=2)
    assert result["status"]=="failed" and result["file_issues"][-1]["code"]=="record_count_mismatch"
    check_counts(result)


def test_ingestion_cli_rejects_unverified_archive_before_creating_output(tmp_path,capsys):
    archive=tmp_path/"synthetic-invalid.zip";archive.write_bytes(b"synthetic invalid archive")
    assert main(["ingest","--archive",str(archive),"--output-root",str(tmp_path/"out")])==1
    assert "Archive mismatch" in capsys.readouterr().err and not (tmp_path/"out").exists()


@pytest.mark.parametrize("fault", ["header", "encoding"])
def test_run_persists_failed_file_instead_of_claiming_complete(tmp_path, fault):
    root = tmp_path / "snapshot"
    root.mkdir()
    path = synth(root)
    if fault == "header":
        path.write_bytes((FIXTURES / "errors/missing_columns/Synthetic/Resources.csv").read_bytes())
    else:
        path.write_bytes(b"%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain\r\n43831,0,0,0,0,\xff,2\r\n")
    manifest = {"snapshot_id": "synthetic", "archive": {"sha256": "b" * 64},
                "files": [{"path": "Resources.csv", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "csv": {"data_rows": 1}}]}
    folder, result = ingest_snapshot(root, manifest, tmp_path / "output")
    assert result["status"] == "failed"
    assert result["file_status_counts"] == {"failed": 1}
    assert not result["files"][0]["complete_for_ingestion"]
    assert json.loads((folder / "manifest.json").read_text())["status"] == "failed"
    check_counts(result["files"][0])
