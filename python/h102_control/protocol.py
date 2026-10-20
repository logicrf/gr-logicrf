"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file protocol.py
@brief Encode/decode bounded JSONL, rejecting duplicate fields, nonfinite numbers and excessive nesting.
@details Control values must be dictionaries containing parameters/session metadata, never executable code.
         Limit frame length before allocating the full frame; errors never echo request contents.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""
import json
import math

MAX_FRAME_BYTES = 2 * 1024 * 1024
"""Maximum UTF-8 frame including newline; accommodates GUI preload of dual 65536-IQ, not continuous data."""
MAX_DEPTH = 32
"""Maximum object/array recursion depth to protect the interpreter stack."""


class ProtocolError(ValueError):
    """Invalid frame/protocol field; fixed error identifiers never include request values."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ProtocolError('CONTROL_API_DUPLICATE_KEY')
        result[key] = value
    return result


def _constant(_):
    raise ProtocolError('CONTROL_API_NONFINITE_NUMBER')


def _check(value, depth=0):
    if depth > MAX_DEPTH:
        raise ProtocolError('CONTROL_API_NESTING_LIMIT')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProtocolError('CONTROL_API_OBJECT_KEY_TYPE')
            _check(child, depth + 1)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _check(child, depth + 1)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError('CONTROL_API_NONFINITE_NUMBER')
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ProtocolError('CONTROL_API_VALUE_TYPE')


def encode_frame(value):
    """Encode one dictionary including newline; reject invalid/oversized values before transmission."""
    if not isinstance(value, dict):
        raise ProtocolError('CONTROL_API_OBJECT_REQUIRED')
    _check(value)
    result = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')) + '\n').encode('utf-8')
    if len(result) > MAX_FRAME_BYTES:
        raise ProtocolError('CONTROL_API_FRAME_LIMIT')
    return result


def decode_frame(data):
    """Parse exactly one frame; reject missing/internal newlines and trailing content outside JSON strings."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_FRAME_BYTES:
        raise ProtocolError('CONTROL_API_FRAME_LIMIT')
    if not data.endswith(b'\n') or b'\n' in data[:-1] or b'\r' in data:
        raise ProtocolError('CONTROL_API_FRAME_DELIMITER')
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                            parse_constant=_constant)
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError('CONTROL_API_INVALID_JSON') from error
    if not isinstance(result, dict):
        raise ProtocolError('CONTROL_API_OBJECT_REQUIRED')
    _check(result)
    return result


def receive_frame(reader):
    """Read one frame from a readline socket stream; EOF/truncation never means empty success."""
    data = reader.readline(MAX_FRAME_BYTES + 1)
    if not data:
        raise EOFError('CONTROL_API_CONNECTION_CLOSED')
    return decode_frame(data)

# End of file: protocol.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
