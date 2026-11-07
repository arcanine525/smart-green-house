"""Pinned BR-03 view composition; no SQL or duplicate duration/water formulas."""
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from greenhouse.app.wiring import WaterAction

CADENCE = timedelta(minutes=5)
TRACE_PAGE_SLOTS = 144
SOURCE_PAGE_SLOTS = 12


def clipped_span(context, start=None, end=None):
    start = max(datetime.combine(context.start, time.min), start or datetime.min)
    end = min(datetime.combine(context.end, time.min), end or datetime.max)
    if start >= end:
        raise ValueError('Khoảng thời gian không giao với cửa sổ báo cáo')
    return start, end


def trace_page(service, context, rule_version, start, end, offset=0):
    """Bound DB fetch AND engine allocation before reading a long episode.

    Offset counts source slots from the floor-grid anchor. Pages meet at grid
    boundaries even when the first requested timestamp is off-grid.
    """
    start, end = clipped_span(context, start, end)
    anchor = start.replace(minute=start.minute // 5 * 5, second=0, microsecond=0)
    total = (end - anchor + CADENCE - timedelta(microseconds=1)) // CADENCE
    last = (total - 1) // TRACE_PAGE_SLOTS * TRACE_PAGE_SLOTS
    offset = min(max(0, offset // TRACE_PAGE_SLOTS * TRACE_PAGE_SLOTS), last)
    page_start = max(start, anchor + offset * CADENCE)
    window = service.get_slot_review(
        context, rule_version=rule_version, window_start=page_start,
        window_end=end, max_slots=TRACE_PAGE_SLOTS)
    return window, offset, total


@dataclass(frozen=True)
class DrainWaterContext:
    action: WaterAction
    overlap_start: datetime
    overlap_end: datetime
    partial_dates: tuple


def daily_water_context(service, evaluation, start=None, end=None):
    """Read each overlapped source DAY exactly once, never per slot/event.

    Midnight is excluded at the right edge. Partial days retain FULL source
    daily values and a label; quantities are neither prorated nor attributed
    causally to the event. With no selected event, use the report window.
    """
    start, end = clipped_span(evaluation.context, start, end)
    day_start = start.date()
    day_end = (end - timedelta(microseconds=1)).date() + timedelta(days=1)
    context = replace(evaluation.context, start=day_start, end=day_end, rule_version=None)
    days = service.get_irrigation_days(context)
    summary = service.get_irrigation_summary(context)
    if len({day.resource_date for day in days}) != len(days):
        raise ValueError('Bối cảnh nước yêu cầu đúng một hàng cho mỗi ngày nguồn')
    if summary.publication != evaluation.publication or any(
            day.publication != evaluation.publication for day in days):
        raise ValueError('Bối cảnh nước phải dùng cùng publication đã pin')
    partial = tuple(day.resource_date for day in days
                    if start > datetime.combine(day.resource_date, time.min)
                    or end < datetime.combine(day.resource_date + timedelta(days=1), time.min))
    return DrainWaterContext(WaterAction(evaluation.publication, context, days, summary, ()),
                             start, end, partial)
