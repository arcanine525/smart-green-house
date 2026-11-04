"""Display-only formatting for DTOs. Rounding happens here and only here;
exports keep exact contract strings (see exports.py)."""
from decimal import Decimal, ROUND_HALF_UP

from greenhouse.contracts import IrrigationDay, IrrigationSummary, QualityStatus

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
