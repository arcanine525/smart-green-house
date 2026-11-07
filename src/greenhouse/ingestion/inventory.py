"""Lexical file inventory only; not typed/quality ingestion or KPI semantics."""

import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from statistics import median


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def excel_display(value):
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).isoformat(sep=" ", timespec="seconds")
    except (OverflowError, ValueError):
        return "outside display range"


def csv_observations(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header_raw = handle.readline().rstrip("\r\n")
        headers = next(csv.reader([header_raw], strict=True))
        empty = [0] * len(headers)
        missing_tokens = [0] * len(headers)
        count, widths, values, previous, deltas = 0, {}, [], None, []
        backwards, repeats, invalid_time = 0, 0, 0
        for row in csv.reader(handle, strict=True):
            count += 1
            widths[str(len(row))] = widths.get(str(len(row)), 0) + 1
            for index in range(len(headers)):
                token = row[index] if index < len(row) else ""
                empty[index] += token.strip() == ""
                missing_tokens[index] += token.strip().lower() in ("", "nan", "na", "n/a")
            raw = row[0] if row else ""
            try:
                current = Decimal(raw)
                if not current.is_finite():
                    raise InvalidOperation
                values.append((current, raw))
                if previous is not None:
                    delta = current - previous
                    if delta > 0:
                        deltas.append(delta)
                    elif delta < 0:
                        backwards += 1
                    else:
                        repeats += 1
                previous = current
            except InvalidOperation:
                invalid_time += 1
                previous = None
        low, high = min(values) if values else None, max(values) if values else None
        return {
            "header_raw": header_raw, "headers": headers, "column_count": len(headers), "data_rows": count,
            "row_width_counts": widths, "empty_cell_counts": empty, "missing_token_counts": missing_tokens,
            "all_empty_column_indices": [i for i, n in enumerate(empty) if n == count and count > 0],
            "time": {"column": headers[0], "min_raw": low[1] if low else None, "max_raw": high[1] if high else None,
                     "display_min": excel_display(low[0]) if low else None, "display_max": excel_display(high[0]) if high else None,
                     "positive_delta_min_days": str(min(deltas)) if deltas else None,
                     "positive_delta_median_days": str(median(deltas)) if deltas else None,
                     "positive_delta_max_days": str(max(deltas)) if deltas else None,
                     "backwards_steps": backwards, "repeated_steps": repeats, "non_numeric_values": invalid_time},
        }


def describe_file(path, relative):
    record = {"path": relative, "size_bytes": path.stat().st_size, "sha256": sha256(path),
              "type": path.suffix.lower().lstrip("."),
              "source_folder": Path(relative).parent.as_posix()}
    if path.suffix.lower() == ".csv":
        record["csv"] = csv_observations(path)
    return record


def summarize_files(files):
    return {"file_count": len(files), "csv_count": sum(f["type"] == "csv" for f in files),
            "pdf_count": sum(f["type"] == "pdf" for f in files),
            "expanded_bytes": sum(f["size_bytes"] for f in files),
            "total_csv_rows": sum(f.get("csv", {}).get("data_rows", 0) for f in files)}


def load_verified_manifest(snapshot):
    snapshot = Path(snapshot)
    if snapshot.is_symlink():
        raise ValueError("Snapshot symlink rejected")
    manifest = json.loads((snapshot / "manifest.json").read_text())
    expected = {entry["path"] for entry in manifest["files"]} | {"manifest.json"}
    actual = {p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in snapshot.rglob("*")):
        raise ValueError("Snapshot file set/symlink mismatch")
    for entry in manifest["files"]:
        relative = entry["path"]
        if Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
            raise ValueError("Unsafe manifest path")
        if describe_file(snapshot / relative, relative) != entry:
            raise ValueError(f"Snapshot differs from manifest: {relative}")
    if any(manifest.get(key) != value for key, value in summarize_files(manifest["files"]).items()):
        raise ValueError("Manifest aggregate counts differ from files")
    return manifest


def render_inventory(manifest):
    """Stable Markdown: acquisition timestamps and machine paths are excluded."""
    lines = ["# Kiểm kê snapshot AGC 2nd edition", "",
             "Số liệu được đo từ snapshot đã khóa; đây là kiểm kê nguồn, không phải dữ liệu typed/quality hoặc bằng chứng nghiệm thu BR.", "",
             f"- Snapshot ID: `{manifest['snapshot_id']}`.",
             f"- Archive: SHA-256 `{manifest['archive']['sha256']}`, {manifest['archive']['size_bytes']:,} byte.",
             f"- {manifest['file_count']} file = {manifest['csv_count']} CSV + {manifest['pdf_count']} PDF; {manifest['expanded_bytes']:,} byte giải nén; tổng {manifest['total_csv_rows']:,} dòng dữ liệu CSV (không tính header).",
             "- Nguồn: [Kaggle AGC](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/). ReadMe.pdf và Economics.pdf được giữ nguyên trong snapshot, có hash/size trong manifest.",
             "- Thư mục nguồn chỉ là định danh thô; mapping khoang/team, timezone, basis diện tích và ngữ nghĩa setpoint/giá thuộc T03. Không tự sửa ngày hoặc gắn UTC.", "",
             "## File, số dòng/cột và thời gian nguồn", "",
             "Thời gian hiển thị dùng epoch Excel 1899-12-30, không timezone, chỉ để đọc inventory. Cadence là median delta dương giữa dòng liên tiếp; không khẳng định mỗi khoảng đều đủ/đều hoặc timestamp đại diện đầu/cuối slot.", "",
             "Median thô 4.9968 phút trong các chuỗi dày tương đương xấp xỉ 5 phút; không làm tròn hoặc sửa serial trong snapshot. Chính sách slot/timezone thuộc T03.", "",
             "| File | Dòng | Cột header | Min serial | Max serial | Min hiển thị | Max hiển thị | Median delta (phút) |",
             "|---|---:|---:|---|---|---|---|---:|"]
    for entry in manifest["files"]:
        if "csv" not in entry:
            continue
        data, time = entry["csv"], entry["csv"]["time"]
        cadence = format(float(Decimal(time["positive_delta_median_days"]) * 1440), ".8g") if time["positive_delta_median_days"] else "—"
        lines.append(f"| `{entry['path']}` | {data['data_rows']} | {data['column_count']} | {time['min_raw']} | {time['max_raw']} | {time['display_min']} | {time['display_max']} | {cadence} |")
    lines += ["", "## PDF nguồn", "", "| File | Byte | SHA-256 |", "|---|---:|---|"]
    for entry in manifest["files"]:
        if entry["type"] == "pdf":
            lines.append(f"| `{entry['path']}` | {entry['size_bytes']} | `{entry['sha256']}` |")
    lines += ["", "## Header nguyên văn và khác biệt", "",
              "Header được lưu nguyên whitespace/tab trong manifest; JSON dưới đây dùng escape để nhìn thấy tab. Không rename, trim, bỏ cột hoặc sửa row ở bước này. Các chỉ số cột dùng index bắt đầu từ 0.", ""]
    groups = {}
    for entry in manifest["files"]:
        if "csv" in entry:
            groups.setdefault((Path(entry["path"]).name, entry["csv"]["header_raw"]), []).append(entry["path"])
    for (kind, header), paths in sorted(groups.items()):
        lines += [f"### {kind} — {len(paths)} file", "", ", ".join(f"`{path}`" for path in paths), "", "```json", json.dumps(header, ensure_ascii=False), "```", ""]
    lines += ["## Quan sát cấu trúc và giá trị trống", "",
              "`missing_token_counts` chỉ đếm token rỗng/NaN/NA/N/A theo chuỗi (không phân loại accepted/invalid/suspect). Row width lệch header được ghi lại, không bỏ dòng. Grain nghiệp vụ và quality policy sẽ được chốt ở T03/T06–T07.", "",
              "| File | Row width → số dòng | Cột toàn rỗng (index) | Time lùi / lặp / không numeric |",
              "|---|---|---|---|"]
    for entry in manifest["files"]:
        if "csv" not in entry:
            continue
        data, time = entry["csv"], entry["csv"]["time"]
        lines.append(f"| `{entry['path']}` | `{json.dumps(data['row_width_counts'], sort_keys=True)}` | `{data['all_empty_column_indices']}` | {time['backwards_steps']} / {time['repeated_steps']} / {time['non_numeric_values']} |")
    lines += ["", "## Token rỗng của các cột phục vụ BR", "",
              "Chỉ liệt kê cột có token rỗng/NaN/NA/N/A; cột không liệt kê có số đếm 0. Đây không phải accepted coverage: giá trị 0, suspect, rule, setpoint off-mode và basis chưa được đánh giá. Manifest giữ count theo index cho tất cả cột.", "",
              "| File | Cột | Số token rỗng | Tổng dòng |", "|---|---|---:|---:|"]
    measures = {"Tair", "CO2air", "t_heat_sp", "t_vent_sp", "co2_sp", "EC_drain_PC", "pH_drain_PC", "Irr", "Drain", "Heat_cons", "ElecHigh", "ElecLow", "ProdA", "ProdB"}
    for entry in manifest["files"]:
        if "csv" in entry:
            data = entry["csv"]
            for column, count in zip(data["headers"], data["missing_token_counts"]):
                if column in measures and count:
                    lines.append(f"| `{entry['path']}` | `{column}` | {count} | {data['data_rows']} |")
    lines += ["", "## Điểm chuyển tiếp cho T03/T06", "",
              "- Climate/Grodan/Weather có grain quan sát theo timestamp; Resources là tổng ngày; Production là record thu hoạch thưa. Không suy coverage harvest bằng số ngày có record.",
              "- Đối chiếu timestamp ngoài cửa sổ vụ ở Production với nguồn gốc; không sửa năm hoặc quarantine bằng inventory. T03 quyết định ngữ nghĩa; T06 giữ source file/dòng.",
              "- Header rỗng dư, tab/whitespace và row width lệch cần adapter có version ở T06. Không coi header cũ trong docs/ref là dictionary hiện hành.",
              "- IUACAAS Resources có 12 cột (5 cột cuối toàn rỗng); Reference TomQuality có 7 trường header nhưng 8 trường mỗi dòng dữ liệu. Các khác biệt này được giữ nguyên; T06 phải quyết định adapter có bằng chứng, không silently truncate.",
              "- Basis nước/nhiệt/điện/Production, timezone/DST, off-mode và biểu giá chưa được xác minh bởi phép kiểm kê này. Không công bố KPI hoặc xếp hạng từ inventory.", "",
              "## Tái tạo", "",
              "Theo [hướng dẫn dataset](dataset-snapshot.md): chạy lệnh inventory trên snapshot đã khóa; output Markdown ổn định, không chứa acquisition time hoặc đường dẫn máy. Manifest giữ acquisition UTC và provenance của lần publish đầu tiên.", ""]
    return "\n".join(lines)
