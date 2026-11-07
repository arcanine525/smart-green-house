"""Lossless storage primitives shared by core, metadata and eventual adapters."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json


def canonical_json(value):
    def encode(item):
        if isinstance(item, Decimal):
            return decimal_text(item)
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(type(item).__name__)
    # Floats are forbidden, including nested values: no binary rounding in payloads.
    def validate(item):
        if isinstance(item, float):
            raise ValueError('Binary float not allowed in exact payload')
        if isinstance(item, dict):
            for v in item.values():
                validate(v)
        elif isinstance(item, (list, tuple)):
            for v in item:
                validate(v)
    validate(value)
    return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def decimal_text(value):
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError('Expected exact decimal string, Decimal or integer')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid decimal') from None
    if not number.is_finite():
        raise ValueError('Nonfinite decimal')
    text = format(number, 'f')
    return (text.rstrip('0').rstrip('.') if '.' in text else text) if number else '0'


def decimal_projection(value):
    if value is None:
        return None, None, 'null'
    exact = decimal_text(value)
    number = Decimal(exact)
    _, digits, exponent = number.as_tuple()
    fractional = max(0, -exponent)
    integer = max(0, len(digits) + exponent)
    if fractional > 38 or integer > 38:
        return exact, None, 'outside_decimal256_projection'
    return exact, exact, 'exact'


def source_clock_us(value):
    """Source-naive microseconds since 1970 without inventing a timezone."""
    if value is None:
        return None
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is not None:
        raise ValueError('Source clock must remain naive')
    delta = stamp - datetime(1970, 1, 1)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
