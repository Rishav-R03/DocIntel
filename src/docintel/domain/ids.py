"""UUIDv7 (RFC 9562): 48-bit Unix-ms timestamp + random bits.

Time-ordered IDs keep B-tree inserts on the right edge of the ``jobs`` primary-key index (no random
page splits like UUIDv4) and make IDs roughly sortable by creation time. Python gains
``uuid.uuid7`` in 3.14; until then this 10-line version is enough. IDs generated within the same
millisecond are not ordered relative to each other, which nothing here relies on.
"""

import os
import time
import uuid

_RAND_BITS = 80
_VERSION_SHIFT = 76  # version nibble sits right after the 48-bit timestamp
_VARIANT_SHIFT = 62


def uuid7() -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000
    value = (unix_ms & ((1 << 48) - 1)) << _RAND_BITS
    value |= int.from_bytes(os.urandom(_RAND_BITS // 8), "big")
    value = (value & ~(0xF << _VERSION_SHIFT)) | (0x7 << _VERSION_SHIFT)
    value = (value & ~(0x3 << _VARIANT_SHIFT)) | (0x2 << _VARIANT_SHIFT)
    return uuid.UUID(int=value)
