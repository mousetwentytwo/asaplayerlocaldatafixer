"""
ASA (Ark Survival Ascended) file readers.

Handles the UE5-based file formats used by ASA, which differ from the
original ASE (Ark Survival Evolved) UE4-based formats in ark.py.

Key differences from ASE format:
  - Header has 3 extra int32 fields and a 16-byte GUID
  - StructProperty/ArrayProperty have flag+name+flag+package sub-type info
  - Properties use index+size+separator ordering (vs size+index in ASE)
  - Extra separator byte (0x00) after each property header
  - New property types: SoftObjectProperty, SetProperty, MapProperty, NameProperty
"""

import io
import struct
import warnings

try:
    from binary import BinaryStream
except ImportError:
    from .binary import BinaryStream


class ASAParseError(Exception):
    """Raised when an ASA file cannot be parsed."""
    pass


def _to_str(value):
    """Ensure a value read from BinaryStream is a Python str (not bytes)."""
    if isinstance(value, bytes):
        return value.decode('ascii', errors='replace')
    return value


def _safe_read_nt_string(stream):
    """Read a null-terminated string, returning '' for zero-length entries."""
    length = stream.readUInt32()
    if length == 0:
        return ''
    raw = stream.readBytes(length - 1)
    stream.readByte()  # null terminator
    if isinstance(raw, bytes):
        return raw.decode('ascii', errors='replace')
    return str(raw)


def _read_byte_val(stream):
    """Read a single byte and return it as an int (0-255)."""
    b = stream.readByte()
    if isinstance(b, bytes):
        return b[0]
    return int(b)


# ---------------------------------------------------------------------------
# Low-level ASA property reading helpers
# ---------------------------------------------------------------------------

def _read_asa_pair(stream):
    """Read a (name, type) pair from an ASA property stream."""
    name = _safe_read_nt_string(stream)
    if name == 'None':
        return (name, None)
    type_name = _safe_read_nt_string(stream)
    return (name, type_name)


def _read_asa_struct_header(stream):
    """
    Read the ASA StructProperty sub-header that appears after the type string.

    Returns (struct_type_name, package_path, index, data_size, tag).
    """
    _flag1 = stream.readInt32()          # always 1 in observed data
    struct_name = _safe_read_nt_string(stream)
    _flag2 = stream.readInt32()          # always 1 in observed data
    package = _safe_read_nt_string(stream)
    index = stream.readInt32()
    data_size = stream.readInt32()
    tag = _read_byte_val(stream)         # property tag byte
    return (struct_name, package, index, data_size, tag)


def _read_asa_array_header(stream):
    """
    Read the ASA ArrayProperty sub-header that appears after the type string.

    Returns (child_type, struct_name, package, index, data_size, tag, array_length).
    struct_name and package will be None for non-struct arrays.
    """
    _flag = stream.readInt32()           # always 1
    child_type = _safe_read_nt_string(stream)
    s_name = None
    package = None
    if child_type == 'StructProperty':
        _flag2 = stream.readInt32()
        s_name = _safe_read_nt_string(stream)
        _flag3 = stream.readInt32()
        package = _safe_read_nt_string(stream)
    index = stream.readInt32()
    data_size = stream.readInt32()
    tag = _read_byte_val(stream)         # property tag byte
    length = stream.readInt32()
    return (child_type, s_name, package, index, data_size, tag, length)


def _read_asa_simple_header(stream):
    """Read index + size + flag for a simple (non-struct/array) property.

    When the flag byte is non-zero there are 4 extra bytes (an additional
    array index) that must be consumed before the property value.
    """
    index = stream.readInt32()
    size = stream.readInt32()
    tag = _read_byte_val(stream)
    extra = None
    if tag:
        extra = stream.readInt32()       # extra array index
    return (index, size, tag, extra)


# ---------------------------------------------------------------------------
# ASA property value readers
# ---------------------------------------------------------------------------

_SIMPLE_READERS = {
    'IntProperty':    lambda s, sz: s.readInt32(),
    'UInt32Property': lambda s, sz: s.readUInt32(),
    'FloatProperty':  lambda s, sz: s.readFloat(),
    'DoubleProperty': lambda s, sz: s.readDouble(),
    'Int64Property':  lambda s, sz: s.readInt64(),
    'UInt64Property': lambda s, sz: s.readUInt64(),
    'Int16Property':  lambda s, sz: s.readInt16(),
    'UInt16Property': lambda s, sz: s.readUInt16(),
}


def _read_asa_property_value(stream, prop_type, index, size):
    """Read a single property value after its header has been consumed."""

    if prop_type in _SIMPLE_READERS:
        return _SIMPLE_READERS[prop_type](stream, size)

    if prop_type == 'BoolProperty':
        if size > 0:
            return bool(stream.readChar())
        return False

    if prop_type == 'StrProperty':
        if size > 0:
            return _safe_read_nt_string(stream)
        return ''

    if prop_type == 'NameProperty':
        if size > 0:
            return _safe_read_nt_string(stream)
        return ''

    if prop_type == 'ByteProperty':
        if size == 1:
            return _read_byte_val(stream)
        if size > 0:
            return stream.readBytes(size)
        return 0

    if prop_type == 'ObjectProperty':
        raw = stream.readBytes(size)
        # Decode common null-reference patterns
        if raw == b'\xff\xff\xff\xff':
            return None
        if raw == b'\x00\x00\x00\x00\xff\xff\xff\xff':
            return None
        # 4-byte non-null: plain object index
        if len(raw) == 4:
            import struct as _struct
            return _struct.unpack_from('<i', raw, 0)[0]
        # Longer: int32(flag) + NTString(blueprint path)
        if len(raw) >= 8:
            import struct as _struct
            flag = _struct.unpack_from('<i', raw, 0)[0]
            slen = _struct.unpack_from('<i', raw, 4)[0]
            if flag >= 0 and 0 < slen < len(raw) and 8 + slen <= len(raw):
                try:
                    path = raw[8:8 + slen - 1].decode('utf-8')
                    return path
                except (UnicodeDecodeError, ValueError):
                    pass
        return raw

    if prop_type == 'SoftObjectProperty':
        # UE5 FSoftObjectPath: PackagePath + AssetName + SubPathString
        package_path = _safe_read_nt_string(stream)
        asset_name = _safe_read_nt_string(stream)
        sub_path = _safe_read_nt_string(stream)
        return {
            'package': package_path,
            'asset': asset_name,
            'sub_path': sub_path,
        }

    # Fallback: read raw bytes
    raw = stream.readBytes(size)
    return raw


# ---------------------------------------------------------------------------
# Recursive ASA struct/property parser
# ---------------------------------------------------------------------------

def parse_asa_properties(stream, data_end=None):
    """
    Parse ASA-format properties from *stream* until a ``None`` terminator
    is encountered or *data_end* is reached.

    Returns an ``OrderedDict``-like plain dict of ``{name: value, ...}``
    where values are either primitives, dicts (structs), or lists (arrays).
    """
    props = {}
    while True:
        if data_end is not None and stream.tell() >= data_end:
            break

        pair_pos = stream.tell()
        try:
            name, ptype = _read_asa_pair(stream)
        except Exception:
            # Hit the edge of readable data – stop gracefully
            if data_end is not None:
                stream.base_stream.seek(data_end)
            break
        if name == 'None':
            break

        try:
            if ptype == 'StructProperty':
                struct_name, package, idx, dsz, tag = _read_asa_struct_header(stream)
                data_start = stream.tell()
                expected_end = data_start + dsz
                inner = parse_asa_properties(stream, data_end=expected_end)

                # If no properties were parsed, the struct likely stores
                # raw data (e.g. Vector, Rotator, Quat).  Preserve the
                # original bytes so the writer can replay them exactly.
                raw_data = None
                if not inner and dsz > 0:
                    stream.base_stream.seek(data_start)
                    raw_data = stream.readBytes(dsz)

                # Safety: ensure stream lands at expected end
                if stream.tell() != expected_end:
                    stream.base_stream.seek(expected_end)
                entry = {
                    '_type': 'StructProperty',
                    '_struct': struct_name,
                    '_package': package,
                    '_index': idx,
                    '_size': dsz,
                    '_tag': tag,
                    'data': inner,
                }
                if raw_data is not None:
                    entry['raw'] = raw_data
                _merge_prop(props, name, entry)

            elif ptype == 'ArrayProperty':
                child_type, s_name, package, idx, dsz, tag, length = \
                    _read_asa_array_header(stream)
                data_start = stream.tell()
                elements, has_sep = _read_asa_array_elements(
                    stream, child_type, s_name, dsz - 4, length, data_start)
                entry = {
                    '_type': 'ArrayProperty',
                    '_child_type': child_type,
                    '_struct': s_name,
                    '_package': package,
                    '_index': idx,
                    '_size': dsz,
                    '_tag': tag,
                    '_has_sep': has_sep,
                    'length': length,
                    'value': elements,
                }
                _merge_prop(props, name, entry)

            elif ptype == 'MapProperty':
                entry = _read_asa_map_property(stream)
                _merge_prop(props, name, entry)

            elif ptype == 'SetProperty':
                entry = _read_asa_set_property(stream)
                _merge_prop(props, name, entry)

            elif ptype == 'BoolProperty':
                # BoolProperty stores its value in the flag byte position;
                # the Size is always 0 and there is no separate value region.
                idx = stream.readInt32()
                _sz = stream.readInt32()      # always 0
                bool_val = _read_byte_val(stream)  # preserve raw byte
                entry = {
                    '_type': 'BoolProperty',
                    '_index': idx,
                    '_size': 0,
                    'value': bool_val,
                }
                _merge_prop(props, name, entry)

            else:
                idx, sz, tag, extra = _read_asa_simple_header(stream)
                value = _read_asa_property_value(stream, ptype, idx, sz)
                entry = {
                    '_type': ptype,
                    '_index': idx,
                    '_size': sz,
                    '_tag': tag,
                    '_extra': extra,
                    'value': value,
                }
                _merge_prop(props, name, entry)

        except Exception:
            # A property failed to parse.  Seek to data_end if known
            # so the caller's stream position stays consistent.
            if data_end is not None:
                stream.base_stream.seek(data_end)
            break

    return props


def _merge_prop(props, name, entry):
    """Handle duplicate property names (indexed properties)."""
    if name in props:
        existing = props[name]
        if isinstance(existing, list):
            existing.append(entry)
        else:
            props[name] = [existing, entry]
    else:
        props[name] = entry


def _read_asa_array_elements(stream, child_type, struct_name,
                             data_bytes, length, data_start):
    """Read *length* array elements of the given child type.

    *data_start* is the stream position where array payload begins
    (right after the length int32).  Used as a safety backstop for
    struct arrays so a single bad element cannot corrupt later parsing.

    Returns ``(elements, has_separators)`` where *has_separators* indicates
    whether 4-byte zero separators were found between struct elements.
    For non-struct arrays *has_separators* is always ``False``.
    """
    data_end = data_start + data_bytes

    if length == 0:
        return ([], False)

    if child_type == 'StructProperty':
        elements = []
        has_separators = None          # detected from first transition
        for i in range(length):
            if i > 0:
                # Peek at next 4 bytes: 0x00000000 means inter-element
                # separator is present; any other value means the bytes
                # are the NTString length of the next property name.
                peek_pos = stream.tell()
                peek_val = stream.readInt32()
                if has_separators is None:
                    # First gap — decide once for the whole array
                    has_separators = (peek_val == 0)
                    if not has_separators:
                        stream.base_stream.seek(peek_pos)
                elif has_separators:
                    pass                   # already consumed
                else:
                    stream.base_stream.seek(peek_pos)
            try:
                inner = parse_asa_properties(stream, data_end=data_end)
                elements.append(inner)
            except Exception:
                # On failure, seek to the data boundary and stop
                stream.base_stream.seek(data_end)
                break
        # Ensure we land at the correct position even if elements
        # consumed fewer or more bytes than expected.
        if stream.tell() != data_end:
            stream.base_stream.seek(data_end)
        return (elements, bool(has_separators) if has_separators is not None
                else False)

    reader_map = {
        'IntProperty':    lambda: stream.readInt32(),
        'UInt32Property': lambda: stream.readUInt32(),
        'FloatProperty':  lambda: stream.readFloat(),
        'DoubleProperty': lambda: stream.readDouble(),
        'Int64Property':  lambda: stream.readInt64(),
        'UInt64Property': lambda: stream.readUInt64(),
        'Int16Property':  lambda: stream.readInt16(),
        'UInt16Property': lambda: stream.readUInt16(),
        'ByteProperty':   lambda: stream.readUChar(),
        'BoolProperty':   lambda: stream.readUChar(),
    }

    if child_type in reader_map:
        return ([reader_map[child_type]() for _ in range(length)], False)

    if child_type in ('StrProperty', 'NameProperty'):
        return ([_safe_read_nt_string(stream)
                for _ in range(length)], False)

    if child_type == 'ObjectProperty':
        elements = []
        for _ in range(length):
            stream.readInt32()  # prefix (always 1)
            elements.append(_safe_read_nt_string(stream))
        return (elements, False)

    if child_type == 'SoftObjectProperty':
        elements = []
        try:
            for _ in range(length):
                # UE5 FSoftObjectPath: PackagePath + AssetName + SubPathString
                package_path = _safe_read_nt_string(stream)
                asset_name = _safe_read_nt_string(stream)
                sub_path = _safe_read_nt_string(stream)
                elements.append({
                    'package': package_path,
                    'asset': asset_name,
                    'sub_path': sub_path,
                })
        except Exception:
            # Format not yet fully understood – skip remaining data
            remaining = data_end - stream.tell()
            if remaining > 0:
                stream.base_stream.seek(data_end)
        return (elements, False)

    # Fallback: read entire remaining data block as raw bytes
    remaining = data_end - stream.tell()
    if remaining > 0:
        return (stream.readBytes(remaining), False)
    return ([], False)


def _read_asa_map_property(stream):
    """Read an ASA MapProperty."""
    _flag_k = stream.readInt32()
    key_type = _safe_read_nt_string(stream)
    _flag_v = stream.readInt32()
    val_type = _safe_read_nt_string(stream)
    index = stream.readInt32()
    size = stream.readInt32()
    tag = _read_byte_val(stream)         # property tag byte
    raw = stream.readBytes(size)
    return {
        '_type': 'MapProperty',
        '_key_type': key_type,
        '_val_type': val_type,
        '_index': index,
        '_size': size,
        '_tag': tag,
        'raw': raw,
    }


def _read_asa_set_property(stream):
    """Read an ASA SetProperty."""
    _flag = stream.readInt32()
    elem_type = _safe_read_nt_string(stream)
    index = stream.readInt32()
    size = stream.readInt32()
    tag = _read_byte_val(stream)         # property tag byte

    if elem_type == 'NameProperty':
        # Parse: 4-byte zero header, 4-byte count, then count NTStrings
        try:
            _zero = stream.readInt32()  # always 0
            count = stream.readInt32()
            names = [_safe_read_nt_string(stream) for _ in range(count)]
            return {
                '_type': 'SetProperty',
                '_elem_type': elem_type,
                '_index': index,
                '_size': size,
                '_tag': tag,
                'value': names,
            }
        except Exception:
            pass  # fall through to raw read

    raw = stream.readBytes(size)
    return {
        '_type': 'SetProperty',
        '_elem_type': elem_type,
        '_index': index,
        '_size': size,
        '_tag': tag,
        'raw': raw,
    }


# ---------------------------------------------------------------------------
# Low-level ASA property writing helpers
# ---------------------------------------------------------------------------


def _write_nt_string(stream, s):
    """Write a length-prefixed null-terminated string (inverse of _safe_read_nt_string)."""
    if not s:
        stream.writeUInt32(0)
        return
    encoded = s.encode('ascii')
    stream.writeUInt32(len(encoded) + 1)
    stream.writeBytes(encoded)
    stream.writeUChar(0)


def _nt_string_byte_size(s):
    """Return the number of bytes a NTString occupies on disk."""
    if not s:
        return 4  # just UInt32(0)
    return 4 + len(s.encode('ascii')) + 1


def _write_asa_pair(stream, name, ptype):
    """Write a (name, type) pair to an ASA property stream."""
    _write_nt_string(stream, name)
    _write_nt_string(stream, ptype)


# ---------------------------------------------------------------------------
# ASA property value writers
# ---------------------------------------------------------------------------

_SIMPLE_WRITERS = {
    'IntProperty':    ('writeInt32', 4),
    'UInt32Property': ('writeUInt32', 4),
    'FloatProperty':  ('writeFloat', 4),
    'DoubleProperty': ('writeDouble', 8),
    'Int64Property':  ('writeInt64', 8),
    'UInt64Property': ('writeUInt64', 8),
    'Int16Property':  ('writeInt16', 2),
    'UInt16Property': ('writeUInt16', 2),
}


def _write_asa_property_value(stream, prop_type, value, size):
    """Write a single property value after its header has been written."""
    if prop_type in _SIMPLE_WRITERS:
        method_name, _ = _SIMPLE_WRITERS[prop_type]
        getattr(stream, method_name)(value)
        return

    if prop_type == 'BoolProperty':
        # Value is stored in the flag byte, not here
        return

    if prop_type in ('StrProperty', 'NameProperty'):
        if size > 0:
            _write_nt_string(stream, value or '')
        return

    if prop_type == 'ByteProperty':
        if isinstance(value, int):
            stream.writeUChar(value)
        elif isinstance(value, str):
            stream.writeBytes(bytes.fromhex(value))
        elif isinstance(value, bytes):
            stream.writeBytes(value)
        return

    if prop_type == 'ObjectProperty':
        if value is None:
            if size == 8:
                stream.writeBytes(b'\x00\x00\x00\x00\xff\xff\xff\xff')
            else:
                stream.writeBytes(b'\xff\xff\xff\xff')
            return
        if isinstance(value, int):
            stream.writeInt32(value)
            return
        if isinstance(value, str):
            stream.writeInt32(1)  # flag
            _write_nt_string(stream, value)
            return

    if prop_type == 'SoftObjectProperty':
        if isinstance(value, dict):
            _write_nt_string(stream, value.get('package', ''))
            _write_nt_string(stream, value.get('asset', ''))
            _write_nt_string(stream, value.get('sub_path', ''))
        return

    # Fallback: write raw bytes
    if isinstance(value, bytes):
        stream.writeBytes(value)
    elif isinstance(value, str):
        stream.writeBytes(bytes.fromhex(value))


def _compute_value_size(prop_type, value, entry):
    """Compute the byte size of a property value for the header."""
    if prop_type in _SIMPLE_WRITERS:
        return _SIMPLE_WRITERS[prop_type][1]

    if prop_type == 'BoolProperty':
        return 0

    if prop_type in ('StrProperty', 'NameProperty'):
        if not value:
            return entry.get('_size', 0)
        return _nt_string_byte_size(value)

    if prop_type == 'ByteProperty':
        if isinstance(value, int):
            return 1
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, str):
            return len(bytes.fromhex(value))
        return 1

    if prop_type == 'ObjectProperty':
        if value is None:
            return entry.get('_size', 4)
        if isinstance(value, int):
            return 4
        if isinstance(value, str):
            encoded = value.encode('utf-8')
            return 4 + 4 + len(encoded) + 1
        return entry.get('_size', 4)

    if prop_type == 'SoftObjectProperty':
        if isinstance(value, dict):
            return (_nt_string_byte_size(value.get('package', ''))
                    + _nt_string_byte_size(value.get('asset', ''))
                    + _nt_string_byte_size(value.get('sub_path', '')))
        return entry.get('_size', 12)

    return entry.get('_size', 0)


# ---------------------------------------------------------------------------
# Array element writer
# ---------------------------------------------------------------------------


def _write_asa_array_elements(stream, child_type, elements, has_sep=True):
    """Write array elements to stream."""
    if not elements:
        return

    if child_type == 'StructProperty':
        for i, elem in enumerate(elements):
            if i > 0 and has_sep:
                stream.writeInt32(0)  # inter-element separator
            serialize_asa_properties(stream, elem)
        return

    _array_writers = {
        'IntProperty':    'writeInt32',
        'UInt32Property': 'writeUInt32',
        'FloatProperty':  'writeFloat',
        'DoubleProperty': 'writeDouble',
        'Int64Property':  'writeInt64',
        'UInt64Property': 'writeUInt64',
        'Int16Property':  'writeInt16',
        'UInt16Property': 'writeUInt16',
        'ByteProperty':   'writeUChar',
    }

    if child_type in _array_writers:
        method_name = _array_writers[child_type]
        for elem in elements:
            getattr(stream, method_name)(elem)
        return

    if child_type == 'BoolProperty':
        for elem in elements:
            stream.writeUChar(elem if isinstance(elem, int) else (1 if elem else 0))
        return

    if child_type in ('StrProperty', 'NameProperty'):
        for elem in elements:
            _write_nt_string(stream, elem)
        return

    if child_type == 'ObjectProperty':
        for elem in elements:
            stream.writeInt32(1)  # prefix
            _write_nt_string(stream, elem)
        return

    if child_type == 'SoftObjectProperty':
        for elem in elements:
            if isinstance(elem, dict):
                _write_nt_string(stream, elem.get('package', ''))
                _write_nt_string(stream, elem.get('asset', ''))
                _write_nt_string(stream, elem.get('sub_path', ''))
        return

    # Fallback: raw bytes
    if isinstance(elements, (bytes, bytearray)):
        stream.writeBytes(elements)
    elif isinstance(elements, str):
        stream.writeBytes(bytes.fromhex(elements))


def _serialize_array_elements(child_type, elements, has_sep=True):
    """Serialize array elements to bytes buffer and return the bytes."""
    buf = io.BytesIO()
    stream = BinaryStream(buf)
    _write_asa_array_elements(stream, child_type, elements, has_sep)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Recursive ASA property serialiser
# ---------------------------------------------------------------------------


# Pre-computed NTString("None") bytes (used for size checks)
_NONE_BYTES = b'\x05\x00\x00\x00None\x00'  # 9 bytes


def _serialize_properties(props, with_none=True):
    """Serialize properties to bytes and return the buffer contents."""
    buf = io.BytesIO()
    stream = BinaryStream(buf)
    serialize_asa_properties(stream, props, with_none=with_none)
    return buf.getvalue()


def serialize_asa_properties(stream, props, with_none=True):
    """Write all properties to *stream*, optionally ending with ``None``."""
    for name, entry in props.items():
        if isinstance(entry, list):
            for e in entry:
                _write_asa_property(stream, name, e)
        else:
            _write_asa_property(stream, name, entry)
    if with_none:
        _write_nt_string(stream, 'None')


def _write_asa_property(stream, name, entry):
    """Write a single property (header + value) to *stream*."""
    ptype = entry.get('_type', '')

    # -- StructProperty ------------------------------------------------
    if ptype == 'StructProperty':
        _write_asa_pair(stream, name, 'StructProperty')
        struct_name = entry.get('_struct', '')
        package = entry.get('_package', '')
        idx = entry.get('_index', 0)
        data = entry.get('data', {})
        orig_size = entry.get('_size', 0)
        raw = entry.get('raw')

        # If the struct was stored as raw bytes (e.g. Vector, Rotator),
        # replay those bytes directly.
        if raw is not None and not data:
            if isinstance(raw, str):
                raw = bytes.fromhex(raw)
            inner_bytes = raw
        else:
            # Serialize inner properties WITHOUT "None" first
            no_none = _serialize_properties(data, with_none=False)

            if orig_size > 0 and len(no_none) + len(_NONE_BYTES) > orig_size:
                # Original struct data cannot fit properties + "None";
                # the original used zero-padding instead of a terminator.
                inner_bytes = no_none + b'\x00' * max(0, orig_size - len(no_none))
            elif orig_size > 0:
                # Use original size to preserve exact byte layout
                with_terminator = no_none + _NONE_BYTES
                if len(with_terminator) <= orig_size:
                    inner_bytes = with_terminator + b'\x00' * (orig_size - len(with_terminator))
                else:
                    inner_bytes = with_terminator
            else:
                inner_bytes = no_none + _NONE_BYTES

        stream.writeInt32(1)               # flag1
        _write_nt_string(stream, struct_name)
        stream.writeInt32(1)               # flag2
        _write_nt_string(stream, package)
        stream.writeInt32(idx)
        stream.writeInt32(len(inner_bytes))  # data_size
        stream.writeUChar(entry.get('_tag', 0))  # property tag byte
        stream.writeBytes(inner_bytes)
        return

    # -- ArrayProperty -------------------------------------------------
    if ptype == 'ArrayProperty':
        _write_asa_pair(stream, name, 'ArrayProperty')
        child_type = entry.get('_child_type', '')
        s_name = entry.get('_struct')
        package = entry.get('_package')
        idx = entry.get('_index', 0)
        elements = entry.get('value', [])
        actual_length = len(elements) if isinstance(elements, list) else 0
        length = actual_length
        elem_bytes = _serialize_array_elements(
            child_type, elements, entry.get('_has_sep', True))
        computed_ds = 4 + len(elem_bytes)    # 4 for length int32
        orig_ds = entry.get('_size', 0)
        # Use the larger of original and computed to prevent truncation.
        # When content is unmodified, computed_ds <= orig_ds (trailing
        # padding is preserved).  When content grew, computed_ds wins.
        data_size = max(orig_ds, computed_ds) if orig_ds > 0 else computed_ds
        stream.writeInt32(1)               # flag
        _write_nt_string(stream, child_type)
        if child_type == 'StructProperty':
            stream.writeInt32(1)           # flag2
            _write_nt_string(stream, s_name or '')
            stream.writeInt32(1)           # flag3
            _write_nt_string(stream, package or '')
        stream.writeInt32(idx)
        stream.writeInt32(data_size)
        stream.writeUChar(entry.get('_tag', 0))
        stream.writeInt32(length)
        stream.writeBytes(elem_bytes)
        pad = (data_size - 4) - len(elem_bytes)
        if pad > 0:
            stream.writeBytes(b'\x00' * pad)
        return

    # -- BoolProperty --------------------------------------------------
    if ptype == 'BoolProperty':
        _write_asa_pair(stream, name, 'BoolProperty')
        idx = entry.get('_index', 0)
        stream.writeInt32(idx)
        stream.writeInt32(0)               # size always 0
        val = entry.get('value', 0)
        stream.writeUChar(val if isinstance(val, int) else (1 if val else 0))
        return

    # -- MapProperty ---------------------------------------------------
    if ptype == 'MapProperty':
        _write_asa_pair(stream, name, 'MapProperty')
        key_type = entry.get('_key_type', '')
        val_type = entry.get('_val_type', '')
        idx = entry.get('_index', 0)
        raw = entry.get('raw', b'')
        if isinstance(raw, str):
            raw = bytes.fromhex(raw)
        stream.writeInt32(1)               # flag_k
        _write_nt_string(stream, key_type)
        stream.writeInt32(1)               # flag_v
        _write_nt_string(stream, val_type)
        stream.writeInt32(idx)
        stream.writeInt32(len(raw))
        stream.writeUChar(entry.get('_tag', 0))  # property tag
        stream.writeBytes(raw)
        return

    # -- SetProperty ---------------------------------------------------
    if ptype == 'SetProperty':
        _write_asa_pair(stream, name, 'SetProperty')
        elem_type = entry.get('_elem_type', '')
        idx = entry.get('_index', 0)
        stag = entry.get('_tag', 0)
        if elem_type == 'NameProperty' and 'value' in entry:
            buf = io.BytesIO()
            bs = BinaryStream(buf)
            bs.writeInt32(0)  # zero header
            names = entry['value']
            bs.writeInt32(len(names))
            for n in names:
                _write_nt_string(bs, n)
            set_data = buf.getvalue()
            stream.writeInt32(1)           # flag
            _write_nt_string(stream, elem_type)
            stream.writeInt32(idx)
            stream.writeInt32(len(set_data))
            stream.writeUChar(stag)        # property tag
            stream.writeBytes(set_data)
        else:
            raw = entry.get('raw', b'')
            if isinstance(raw, str):
                raw = bytes.fromhex(raw)
            stream.writeInt32(1)           # flag
            _write_nt_string(stream, elem_type)
            stream.writeInt32(idx)
            stream.writeInt32(len(raw))
            stream.writeUChar(stag)        # property tag
            stream.writeBytes(raw)
        return

    # -- Simple properties (Int, Float, Str, Name, Object, etc.) -------
    _write_asa_pair(stream, name, ptype)
    idx = entry.get('_index', 0)
    value = entry.get('value')
    sz = _compute_value_size(ptype, value, entry)
    tag = entry.get('_tag', 0)
    stream.writeInt32(idx)
    stream.writeInt32(sz)
    stream.writeUChar(tag)
    if tag:
        extra = entry.get('_extra')
        stream.writeInt32(extra if extra is not None else 0)
    _write_asa_property_value(stream, ptype, value, sz)


# ---------------------------------------------------------------------------
# PlayerLocalData reader
# ---------------------------------------------------------------------------

class PlayerLocalData:
    """
    Reads and exposes the contents of a ``PlayerLocalData.arkprofile``
    file from *Ark: Survival Ascended* (ASA).

    This file uses UE5-based serialisation and contains local player
    inventory data such as tribute items, uploaded dinos, engram unlocks,
    achievements, and explorer notes.

    Example usage::

        from arkplayerlocaldatafixer.asa import PlayerLocalData

        pld = PlayerLocalData('PlayerLocalData.arkprofile')
        print(pld.map_name)
        print(pld.ark_items)
        print(pld.tamed_dinos)
        for a in pld.achievements:
            print(a)
    """

    def __init__(self, file_path=None):
        # Header fields
        self.header_v1 = 0
        self.header_v2 = 0
        self.header_v3 = 0
        self.version = 1
        self.guid = b'\x00' * 16
        self.file_type = ''
        self.name = ''
        self.controller = ''
        self.game_mode = 'PersistentLevel'
        self.map_name = ''
        self.map_path = ''
        self.header_size = 0

        # Parsed property tree
        self.data = {}

        # Raw trailing bytes that follow the property section
        self.trailing_data = b''

        if file_path is not None:
            self._load(file_path)

    # -- public convenience properties --------------------------------------

    @property
    def ark_data(self):
        """The top-level ``MyArkData`` struct dict, or ``{}``."""
        md = self.data.get('MyArkData', {})
        if isinstance(md, dict):
            return md.get('data', {})
        return {}

    @property
    def ark_items(self):
        """List of ark tribute inventory items (dicts or raw)."""
        ai = self.ark_data.get('ArkItems', {})
        if isinstance(ai, dict):
            return ai.get('value', [])
        return []

    @property
    def tamed_dinos(self):
        """List of ark tribute tamed dinos (dicts or raw)."""
        td = self.ark_data.get('ArkTamedDinosData', {})
        if isinstance(td, dict):
            return td.get('value', [])
        return []

    @property
    def club_ark_tokens(self):
        """Integer count of ClubArkTokens, or 0."""
        ct = self.ark_data.get('ClubArkTokens', {})
        if isinstance(ct, dict):
            return ct.get('value', 0)
        return 0

    @property
    def custom_cloud_data(self):
        """List of custom cloud data entries."""
        cd = self.ark_data.get('CustomCloudDatas', {})
        if isinstance(cd, dict):
            return cd.get('value', [])
        return []

    @property
    def persistent_item_unlocks(self):
        """List of persistent item unlock paths."""
        pi = self.ark_data.get('PersistentItemUnlocks', {})
        if isinstance(pi, dict):
            return pi.get('value', [])
        return []

    @property
    def achievements(self):
        """List of unlocked achievement strings."""
        ua = self.data.get('UnlockedAchievements', {})
        if isinstance(ua, dict):
            return ua.get('value', [])
        return []

    @property
    def achievement_items(self):
        """List of achievement item paths collected."""
        ai = self.data.get('AchievementItemsCollectedList', {})
        if isinstance(ai, dict):
            return ai.get('value', [])
        return []

    @property
    def explorer_note_unlocks(self):
        """List of global explorer note unlock IDs."""
        en = self.data.get('GlobalExplorerNoteUnlocks', {})
        if isinstance(en, dict):
            return en.get('value', [])
        return []

    @property
    def named_explorer_note_unlocks(self):
        """List of named explorer note unlock strings."""
        gn = self.data.get('GlobalNamedExplorerNoteUnlocks', {})
        if isinstance(gn, dict):
            return gn.get('value', [])
        return []

    @property
    def tamed_dino_tags(self):
        """List of tamed dino tag strings."""
        td = self.data.get('TamedDinoTags', {})
        if isinstance(td, dict):
            return td.get('value', [])
        return []

    @property
    def fog_of_wars(self):
        """List of per-map fog of war dicts."""
        fw = self.data.get('PerMapFogOfWars', {})
        if isinstance(fw, dict):
            return fw.get('value', [])
        return []

    @property
    def map_markers(self):
        """List of per-map map marker dicts."""
        mm = self.data.get('MapMarkersPerMaps', {})
        if isinstance(mm, dict):
            return mm.get('value', [])
        return []

    @property
    def saved_favorites_version(self):
        """Saved favorites version integer."""
        sv = self.data.get('SavedFavoritesVersion', {})
        if isinstance(sv, dict):
            return sv.get('value', 0)
        return 0

    # -- internal -----------------------------------------------------------

    def _load(self, file_path):
        with open(file_path, 'rb') as ifile:
            stream = BinaryStream(ifile)

            # --- ASA extended header ---
            self.header_v1 = stream.readInt32()
            self.header_v2 = stream.readInt32()
            self.header_v3 = stream.readInt32()
            self.version = stream.readInt32()

            if self.version != 1:
                raise ASAParseError(
                    'Unexpected version %d (expected 1)' % self.version)

            self.guid = stream.readBytes(16)
            self.file_type = _safe_read_nt_string(stream)
            stream.readInt32()  # always 0
            stream.readInt32()  # always 5
            self.name = _safe_read_nt_string(stream)
            self.controller = _safe_read_nt_string(stream)
            self.game_mode = _safe_read_nt_string(stream)
            self.map_name = _safe_read_nt_string(stream)
            self.map_path = _safe_read_nt_string(stream)
            stream.readBytes(12)  # 12 zero bytes
            self.header_size = stream.readInt32()
            stream.readInt32()    # always 0
            stream.readByte()     # ASA extra separator byte (0x00)

            # --- Properties section ---
            self.data = parse_asa_properties(stream)

            # --- Trailing data (raw) ---
            pos = stream.tell()
            ifile.seek(0, 2)
            end = ifile.tell()
            if pos < end:
                ifile.seek(pos)
                self.trailing_data = ifile.read()

    def __repr__(self):
        return '<PlayerLocalData %s map=%s items=%d dinos=%d>' % (
            self.name, self.map_name,
            len(self.ark_items), len(self.tamed_dinos))

    # -- helpers for exploring data -----------------------------------------

    def keys(self):
        """Return the top-level property names."""
        return list(self.data.keys())

    def get(self, key, default=None):
        """Get a top-level property by name."""
        return self.data.get(key, default)

    # -- serialisation helpers ----------------------------------------------

    @staticmethod
    def _jsonify(obj):
        """Recursively convert parsed data tree to JSON-safe types."""
        if isinstance(obj, dict):
            return {k: PlayerLocalData._jsonify(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [PlayerLocalData._jsonify(v) for v in obj]
        if isinstance(obj, bytes):
            # Try to present bytes as hex string
            return obj.hex()
        if isinstance(obj, float):
            import math, struct as _st
            if math.isnan(obj) or math.isinf(obj):
                return {'__special_float__': _st.pack('<d', obj).hex()}
            return obj
        if isinstance(obj, (int, str, bool)):
            return obj
        if obj is None:
            return None
        return str(obj)

    def to_dict(self):
        """Return the full parsed data as a JSON-serialisable dict."""
        return {
            'header': {
                'file_type': self.file_type,
                'name': self.name,
                'controller': self.controller,
                'game_mode': self.game_mode,
                'map_name': self.map_name,
                'map_path': self.map_path,
                'version': self.version,
                'guid': self.guid.hex(),
                'header_v1': self.header_v1,
                'header_v2': self.header_v2,
                'header_v3': self.header_v3,
                'header_size': self.header_size,
                'trailing_data': self.trailing_data.hex(),
            },
            'data': self._jsonify(self.data),
        }

    def to_json(self, indent=2):
        """Return the full parsed data as a JSON string."""
        import json
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    # -- binary writer --------------------------------------------------

    def save(self, file_path):
        """Write the complete data back to an ``.arkprofile`` binary file.

        Sizes and lengths are recalculated from actual content before
        writing, so manual ``_size`` / ``length`` bookkeeping is not
        required after editing.
        """
        self.recalculate_sizes()
        with open(file_path, 'wb') as ofile:
            stream = BinaryStream(ofile)

            # --- ASA extended header ---
            stream.writeInt32(self.header_v1)
            stream.writeInt32(self.header_v2)
            stream.writeInt32(self.header_v3)
            stream.writeInt32(self.version)
            stream.writeBytes(self.guid)
            _write_nt_string(stream, self.file_type)
            stream.writeInt32(0)
            stream.writeInt32(5)
            _write_nt_string(stream, self.name)
            _write_nt_string(stream, self.controller)
            _write_nt_string(stream, self.game_mode)
            _write_nt_string(stream, self.map_name)
            _write_nt_string(stream, self.map_path)
            stream.writeBytes(b'\x00' * 12)
            stream.writeInt32(self.header_size)
            stream.writeInt32(0)
            stream.writeUChar(0)   # ASA extra separator byte

            # --- Properties section ---
            serialize_asa_properties(stream, self.data)

            # --- Trailing data ---
            stream.writeBytes(self.trailing_data)

    # -- reconstruction from dict / JSON --------------------------------

    def recalculate_sizes(self):
        """Walk the property tree and update every ``_size`` and ``length``
        field to match the actual serialised content.

        Called automatically by :meth:`save`, but can be invoked manually
        after programmatic edits to keep the dict representation consistent.
        """
        self.data = self._recalc(self.data)

    @classmethod
    def _recalc(cls, props):
        """Recursively recalculate sizes for a property dict."""
        for name, entry in list(props.items()):
            if isinstance(entry, list):
                props[name] = [cls._recalc_entry(e) for e in entry]
            elif isinstance(entry, dict) and '_type' in entry:
                props[name] = cls._recalc_entry(entry)
        return props

    @classmethod
    def _recalc_entry(cls, entry):
        ptype = entry.get('_type', '')

        if ptype == 'StructProperty':
            data = entry.get('data', {})
            if data:
                cls._recalc(data)
                raw = entry.get('raw')
                if raw is not None and not data:
                    if isinstance(raw, str):
                        raw = bytes.fromhex(raw)
                    inner_bytes = raw
                else:
                    no_none = _serialize_properties(data, with_none=False)
                    orig = entry.get('_size', 0)
                    if orig > 0 and len(no_none) + len(_NONE_BYTES) > orig:
                        inner_bytes = no_none + b'\x00' * max(0, orig - len(no_none))
                    elif orig > 0:
                        wt = no_none + _NONE_BYTES
                        inner_bytes = wt + b'\x00' * max(0, orig - len(wt)) if len(wt) <= orig else wt
                    else:
                        inner_bytes = no_none + _NONE_BYTES
                entry['_size'] = len(inner_bytes)

        elif ptype == 'ArrayProperty':
            elements = entry.get('value', [])
            actual_len = len(elements) if isinstance(elements, list) else 0
            entry['length'] = actual_len
            child_type = entry.get('_child_type', '')
            if child_type == 'StructProperty' and isinstance(elements, list):
                for elem in elements:
                    if isinstance(elem, dict):
                        cls._recalc(elem)
            elem_bytes = _serialize_array_elements(
                child_type, elements, entry.get('_has_sep', True))
            computed = 4 + len(elem_bytes)
            orig = entry.get('_size', 0)
            entry['_size'] = max(orig, computed) if orig > 0 else computed

        elif ptype == 'MapProperty':
            raw = entry.get('raw', b'')
            if isinstance(raw, str):
                raw = bytes.fromhex(raw)
            entry['_size'] = len(raw)

        elif ptype == 'SetProperty':
            elem_type = entry.get('_elem_type', '')
            if elem_type == 'NameProperty' and 'value' in entry:
                buf = io.BytesIO()
                bs = BinaryStream(buf)
                bs.writeInt32(0)
                names = entry['value']
                bs.writeInt32(len(names))
                for n in names:
                    _write_nt_string(bs, n)
                entry['_size'] = buf.tell()
            # else: raw — _size already matches len(raw)

        elif ptype != 'BoolProperty':
            value = entry.get('value')
            entry['_size'] = _compute_value_size(ptype, value, entry)

        return entry

    @classmethod
    def from_dict(cls, d):
        """Create a ``PlayerLocalData`` from a dict (as produced by ``to_dict()``)."""
        obj = cls()
        header = d.get('header', {})
        obj.file_type = header.get('file_type', '')
        obj.name = header.get('name', '')
        obj.controller = header.get('controller', '')
        obj.game_mode = header.get('game_mode', 'PersistentLevel')
        obj.map_name = header.get('map_name', '')
        obj.map_path = header.get('map_path', '')
        obj.version = header.get('version', 1)
        obj.guid = bytes.fromhex(header.get('guid', '00' * 16))
        obj.header_v1 = header.get('header_v1', 0)
        obj.header_v2 = header.get('header_v2', 0)
        obj.header_v3 = header.get('header_v3', 0)
        obj.header_size = header.get('header_size', 0)
        td = header.get('trailing_data', '')
        if td:
            obj.trailing_data = bytes.fromhex(td)
        else:
            obj.trailing_data = struct.pack('<I', 0) + obj.guid
        obj.data = cls._unjsonify(d.get('data', {}))
        return obj

    @classmethod
    def from_json(cls, json_str):
        """Create a ``PlayerLocalData`` from a JSON string."""
        import json
        return cls.from_dict(json.loads(json_str))

    @staticmethod
    def _unjsonify(obj):
        """Reverse ``_jsonify``: convert hex strings back to bytes for raw fields."""
        if isinstance(obj, dict):
            if '__special_float__' in obj:
                import struct as _st
                return _st.unpack('<d', bytes.fromhex(obj['__special_float__']))[0]
            result = {}
            for k, v in obj.items():
                if k == 'raw' and isinstance(v, str):
                    try:
                        result[k] = bytes.fromhex(v)
                    except ValueError:
                        result[k] = v
                else:
                    result[k] = PlayerLocalData._unjsonify(v)
            return result
        if isinstance(obj, list):
            return [PlayerLocalData._unjsonify(v) for v in obj]
        return obj
