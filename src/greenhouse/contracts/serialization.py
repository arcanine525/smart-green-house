"""Versioned JSON wire format: exact decimal strings, explicit ISO clocks, enums.

The caller supplies the DTO class. Unknown fields/types/versions and numeric JSON
values for Decimal are rejected; decoding never guesses an untagged DTO type.
"""
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from greenhouse.contracts import models

CONTRACT_VERSION = 1


def to_dict(value):
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Nonfinite Decimal cannot be serialized')
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and value.__class__.__module__ == models.__name__:
        return {f.name: to_dict(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [to_dict(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError('Unsupported contract value')


def _decode(kind, value):
    origin = get_origin(kind)
    if origin is UnionType:
        choices = get_args(kind)
        if value is None and type(None) in choices:
            return None
        return _decode(next(k for k in choices if k is not type(None)), value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError('Expected JSON array')
        return tuple(_decode(get_args(kind)[0], item) for item in value)
    if kind is Decimal:
        if not isinstance(value, str):
            raise ValueError('Decimal must be an exact JSON string, not a number')
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ValueError('Invalid decimal string') from error
        if not result.is_finite():
            raise ValueError('Nonfinite Decimal')
        return result
    if kind in (date, datetime):
        if not isinstance(value, str):
            raise ValueError('Expected ISO date/time string')
        return kind.fromisoformat(value)
    if isinstance(kind, type) and issubclass(kind, StrEnum):
        return kind(value)
    if is_dataclass(kind) and kind.__module__ == models.__name__:
        if not isinstance(value, dict) or value.keys() != {f.name for f in fields(kind)}:
            raise ValueError('Contract fields do not match DTO')
        hints = get_type_hints(kind)
        return kind(**{key: _decode(hints[key], item) for key, item in value.items()})
    if type(value) is not kind or kind not in (str, int, bool):
        raise ValueError('Incorrect contract scalar type')
    return value


def from_dict(dto_type, payload):
    if not is_dataclass(dto_type) or dto_type.__module__ != models.__name__:
        raise ValueError('Expected a public contract DTO class')
    return _decode(dto_type, payload)


def to_json(value):
    return json.dumps({'contract_version': CONTRACT_VERSION, 'data': to_dict(value)}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def from_json(dto_type, text):
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.keys() != {'contract_version', 'data'} or type(payload['contract_version']) is not int or payload['contract_version'] != CONTRACT_VERSION:
        raise ValueError('Unsupported contract envelope/version')
    return from_dict(dto_type, payload['data'])
