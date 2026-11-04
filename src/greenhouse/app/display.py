"""Display-only formatting for DTOs. Rounding happens here and only here;
exports keep exact contract strings (see exports.py)."""
from decimal import Decimal, ROUND_HALF_UP

from greenhouse.contracts import (
    HARVEST_UNIT, HarvestDay, HarvestRecord, IrrigationDay, IrrigationSummary, QualityStatus,
    UnresolvedSourceAudit,
)

NULL_TEXT = '—'

REASON_GLOSS = {
    'missing_resource_day': 'Ngày không có bản ghi Resources',
    'no_accepted_irrigation': 'Không có ngày Irr accepted',
    'no_accepted_drain': 'Không có ngày Drain accepted',
    'no_paired_days': 'Không có ngày paired (cả hai measure dùng được, basis tương thích)',
    'no_irrigation_quantity': 'Tổng Irr trên các ngày paired bằng 0 → tỷ lệ không xác định',
    'partial_coverage': 'Kết quả trên phần ngày có dữ liệu, không ngoại suy toàn kỳ',
    'window_clipped_to_cycle': 'Cửa sổ yêu cầu đã bị cắt theo biên vụ',
    'incompatible_or_unknown_basis': 'Basis diện tích Irr/Drain chưa tương thích/chưa xác định → tỷ lệ NULL',
    'suspect_values_excluded': 'Có ngày chứa giá trị suspect (giữ trạng thái, không thành 0)',
    'drain_exceeds_irrigation': 'Drain > Irr trong ngày — giữ tỷ lệ thật, cần rà soát',
    'outside_irrigation_rule': 'Ngày ngoài dải rule đã chọn',
    'irrigation_rule_unknown': 'Ngày không xác định được trạng thái rule (thiếu cặp hoặc Irr=0)',
}

STATUS_GLOSS = {
    'complete': 'Đầy đủ',
    'partial': 'Một phần',
    'unknown': 'Chưa xác định',
}

QUALITY_GLOSS = {
    QualityStatus.ACCEPTED: 'accepted',
    QualityStatus.MISSING: 'missing (thiếu)',
    QualityStatus.INVALID: 'invalid (lỗi)',
    QualityStatus.SUSPECT: 'suspect (nghi lỗi)',
}


def format_decimal(value, digits=2):
    """UI rounding only; None stays an explicit dash, 0 stays visible."""
    if value is None:
        return NULL_TEXT
    if not isinstance(value, Decimal):
        return str(value)
    exponent = Decimal(1).scaleb(-digits)
    return f'{value.quantize(exponent, rounding=ROUND_HALF_UP):f}'.replace('.', ',')


def gloss_reason(code):
    return f'{code} — {REASON_GLOSS[code]}' if code in REASON_GLOSS else code


def gloss_reasons(codes):
    return [gloss_reason(code) for code in codes]


def join(values, separator='; '):
    if values is None:
        return ''
    return separator.join(str(item) for item in values)


def window_label(summary: IrrigationSummary):
    """Half-open effective window, labeled for operators."""
    return f'{summary.window_start.isoformat()} → {summary.window_end.isoformat()} (nửa mở)'


def day_row(day: IrrigationDay):
    """Formatted table row mirroring the displayed day DTO."""
    return {
        'Ngày': day.resource_date.isoformat(),
        'Irr (L/m²)': format_decimal(day.irrigation.value),
        'Drain (L/m²)': format_decimal(day.drain.value),
        'Chất lượng Irr': QUALITY_GLOSS[day.irrigation.quality],
        'Chất lượng Drain': QUALITY_GLOSS[day.drain.quality],
        'Tỷ lệ drain (%)': format_decimal(day.drain_fraction_pct),
        'Paired': 'có' if day.paired else 'không',
        'Rule': day.rule_status.value,
        'Lý do / trạng thái': join(day.reason_codes),
        'Revision': day.revision_id or '',
    }


def weekly_row(summary: IrrigationSummary):
    """Formatted weekly row; numerator/denominator stay visible."""
    label = (f'{summary.iso_year}-W{summary.iso_week:02d}'
             if summary.iso_year is not None else 'Toàn kỳ đã chọn')
    return {
        'Tuần': label,
        'Cửa sổ': f'{summary.window_start.isoformat()} → {summary.window_end.isoformat()}',
        'Ngày kỳ vọng': summary.expected_days,
        'Irr observed (L/m²)': format_decimal(summary.irrigation_observed_l_m2),
        'Drain observed (L/m²)': format_decimal(summary.drain_observed_l_m2),
        'Paired Irr (L/m²)': format_decimal(summary.paired_irrigation_l_m2),
        'Paired Drain (L/m²)': format_decimal(summary.paired_drain_l_m2),
        'Tỷ lệ drain (%)': format_decimal(summary.drain_fraction_pct),
        'Độ phủ paired (%)': format_decimal(summary.paired_coverage_pct),
        'Trạng thái': STATUS_GLOSS[summary.status.value],
        'Lý do': join(summary.reason_codes),
    }


HARVEST_REASON_GLOSS = {
    'incomplete_snapshot': 'Snapshot nguồn chưa hoàn chỉnh — không xếp hạng',
    'missing_expected_source_file': 'Thiếu file nguồn kỳ vọng của vụ',
    'ingestion_file_not_loaded': 'File nguồn chưa được nạp đúng trạng thái',
    'unresolved_quarantine_records': 'Còn chứa record quarantine chưa phân giải',
    'unresolved_conflict_records': 'Còn chứa record conflict chưa phân giải',
    'records_without_paired_measures': 'Có record thiếu một trong hai measure dùng được',
    'known_zero_event_window': 'Cửa sổ trống đã xác minh (snapshot đầy đủ) — known 0',
    'known_zero_harvest_day': 'Ngày không có thu hoạch đã xác minh — không phải thiếu dữ liệu',
    'unverified_no_harvest_record': 'Không có record nguồn trong ngày; chưa xác minh được là không thu',
    'no_paired_mass': 'Khối lượng paired bằng 0 — tỷ lệ không xác định',
    'partial_paired_records': 'Chỉ một phần record đủ A và B dùng được',
    'records_without_usable_measures': 'Không có measure nào dùng được trong các record của cửa sổ',
    'unranked_incomplete_compartment': 'Khoang chưa đủ điều kiện xếp hạng (completeness chưa đạt)',
    'outside_harvest_rule': 'Lần thu/tuần vượt ngưỡng class-B của rule đã chọn',
    'harvest_rule_unknown': 'Không xác định được trạng thái rule (thiếu cặp hoặc khối lượng 0)',
    'cumulative_from_cycle_start': 'Lũy kế tính từ đầu vụ (origin hiển thị riêng)',
    'window_clipped_to_cycle': 'Cửa sổ yêu cầu đã bị cắt theo biên vụ',
    'source_resolved_but_unpaired': 'Nguồn đã phân giải; sai lệch nằm ở cặp A/B',
}


def gloss_harvest_reason(code):
    return f'{code} — {HARVEST_REASON_GLOSS[code]}' if code in HARVEST_REASON_GLOSS else code


def gloss_harvest_reasons(codes):
    return [gloss_harvest_reason(code) for code in codes]


def harvest_record_row(record: HarvestRecord):
    """Record-grain row mirroring the displayed record DTO (no recomputation)."""
    return {
        'Ngày': record.harvest_date.isoformat(),
        'ProdA (kg/m²)': format_decimal(record.prod_a.value),
        'ProdB (kg/m²)': format_decimal(record.prod_b.value),
        'Chất lượng A': QUALITY_GLOSS[record.prod_a.quality],
        'Chất lượng B': QUALITY_GLOSS[record.prod_b.quality],
        'Share B (%)': format_decimal(record.class_b_share_pct),
        'Paired': 'có' if record.paired else 'không',
        'Rule': record.rule_status.value,
        'Correction': f'{record.correction_id} v{record.correction_version}' if record.correction_id else '',
        'Revision': record.revision_id,
        'Lý do': join(record.reason_codes),
    }


def harvest_day_row(day: HarvestDay):
    """Day-grain row; NULL là trống/dash, known-zero là 0 hiển thị."""
    return {
        'Ngày': day.harvest_date.isoformat(),
        'A observed (kg/m²)': format_decimal(day.prod_a),
        'B observed (kg/m²)': format_decimal(day.prod_b),
        'Paired A (kg/m²)': format_decimal(day.paired_prod_a),
        'Paired B (kg/m²)': format_decimal(day.paired_prod_b),
        'Share B (%)': format_decimal(day.class_b_share_pct),
        'Số record': day.record_count,
        'Paired records': day.paired_records,
        'Lũy kế A từ đầu vụ': format_decimal(day.cumulative_a),
        'Lũy kế total từ đầu vụ': format_decimal(day.cumulative_total),
        'Basis ngày': day.basis_status.value + (f' · {day.basis_label}' if day.basis_label else ''),
        'Rule': day.rule_status.value,
        'Lý do': join(day.reason_codes),
    }


def harvest_weekly_row(summary):
    """Weekly harvest row over the same summary contract."""
    label = (f'{summary.iso_year}-W{summary.iso_week:02d}'
             if summary.iso_year is not None else 'Toàn kỳ đã chọn')
    return {
        'Tuần': label,
        'Cửa sổ': f'{summary.window_start.isoformat()} → {summary.window_end.isoformat()}',
        'A observed (kg/m²)': format_decimal(summary.prod_a_observed),
        'B observed (kg/m²)': format_decimal(summary.prod_b_observed),
        'Paired A (kg/m²)': format_decimal(summary.paired_prod_a),
        'Paired B (kg/m²)': format_decimal(summary.paired_prod_b),
        'Share A (%)': format_decimal(summary.class_a_share_pct),
        'Share B (%)': format_decimal(summary.class_b_share_pct),
        'Lũy kế A': format_decimal(summary.cumulative_a),
        'Lũy kế total': format_decimal(summary.cumulative_total),
        'Origin lũy kế': summary.cumulative_origin.isoformat() if summary.cumulative_origin else '',
        'Trạng thái': STATUS_GLOSS[summary.status.value],
        'Rule': summary.rule_status.value,
        'Lý do': join(summary.reason_codes),
    }


def unresolved_row(item):
    """Typed unresolved issue row; quarantined numbers are never invented here."""
    return {
        'Source record': item.source_record_id,
        'Loại': item.disposition,
        'Reason': item.reason_code,
        'Cột': item.issue_column or '',
        'File': item.relative_path or '',
        'Dòng': item.line_number or '',
        'Serial nguồn': format_decimal(item.source_time_serial),
    }


AUDIT_NOTE_GLOSS = {
    'raw-mount-missing': 'Raw mount không cấu hình/không đọc được — không xác minh được',
    'raw-file-missing': 'File raw không có trên mount — không xác minh được',
    'raw-file-unreadable': 'Raw mount không đọc được — không xác minh được',
    'raw-identity-mismatch': 'File/record raw hoặc checksum không khớp — không xác minh được',
    'raw-token-shape-mismatch': 'Dòng raw lệch tiêu chuẩn — không hiển thị giá trị',
}


def source_audit_rows(audit: UnresolvedSourceAudit):
    """Audit display: only VERIFIED audits expose raw token values."""
    cells = list(zip(audit.headers, audit.tokens))
    return [{'Cột': column, 'Giá trị raw (nguyên văn)': token}
            for column, token in cells]
