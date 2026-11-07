"""Replay source times use the same five-minute policy as the AGC parser."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from greenhouse.ingestion.parser import EPOCH


def slot_from_serial(serial):
    try:
        value = Decimal(serial)
        if not value.is_finite():
            raise ValueError('nonfinite source serial')
        index = (value * 288).to_integral_value(rounding=ROUND_HALF_EVEN)
        if abs(value * 288 - index) * 300 > Decimal('0.5'):
            raise ValueError('source timestamp is outside five-minute tolerance')
        return EPOCH + timedelta(minutes=int(index) * 5)
    except (InvalidOperation, OverflowError, TypeError) as error:
        raise ValueError('invalid source serial') from error


def normalize_cursor(source_time):
    value = datetime.fromisoformat(source_time)
    if value.tzinfo is not None:
        raise ValueError('source time must be naive')
    elapsed = value - EPOCH
    micros = ((elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds)
    step = 300 * 1000000
    index = (Decimal(micros) / step).to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(Decimal(micros) - index * step) > 500000:
        raise ValueError('cursor timestamp is outside five-minute tolerance')
    return EPOCH + timedelta(minutes=int(index) * 5)


def event_slot(envelope):
    slot = slot_from_serial(envelope['source_provenance']['source_time_serial'])
    observed = normalize_cursor(envelope['source_observed_at'])
    if observed != slot:
        raise ValueError('source time and serial resolve to different slots')
    declared = envelope['source_provenance'].get('slot_start')
    if declared is not None and datetime.fromisoformat(declared) != slot:
        raise ValueError('declared slot differs from source serial')
    return slot
