"""Offline LK byte preparation; no acquisition, package admission or device I/O.

Originals must later match the complete cold backups before the first installer
write. These hashes bind bytes only; they do not attest freshness, device identity,
an unlocked bootloader, selector preservation, or recoverability after a boot.
"""
from dataclasses import dataclass
import hashlib

PARTITION_BYTES = 1048576
PAYLOAD_BYTES = 864000
STOCK_SHA256 = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
PATCHED_SHA256 = 'e4299344da5d5dd1a202b4964de764c43557f97203799e77bab55ac962973de0'
SLOTS = ('lk_a', 'lk_b')


class InvalidLK(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise InvalidLK(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def profile(original):
    require(type(original) is bytes and len(original) == PARTITION_BYTES,
            'Expected a complete immutable 1 MiB LK original')
    prefix = sha(original[:PAYLOAD_BYTES])
    if prefix == STOCK_SHA256:
        return 'stock-v0.8.293'
    if prefix == PATCHED_SHA256:
        return 'current-patched'
    if original == bytes(PARTITION_BYTES):
        return 'blank-zero'
    if original == b'\xff' * PARTITION_BYTES:
        return 'blank-ff'
    raise InvalidLK('Unknown, partial, or mixed LK original')


@dataclass(frozen=True)
class PreparedSlots:
    images: tuple[tuple[str, bytes], ...]
    originals: tuple[tuple[str, str], ...]
    profiles: tuple[tuple[str, str], ...]


def prepare(original_a, original_b, patched):
    """Preserve each slot's exact tail after the reviewed same-size LK prefix.

    One uniformly blank slot is supported only alongside a recognized stock or
    current patched prefix in the other slot. Arbitrary older/custom/partial LK
    is rejected. Accepting a blank slot does not promise that the original pair
    can boot or that restoring it is independent of the hardware selector.
    """
    require(type(patched) is bytes and len(patched) == PAYLOAD_BYTES and
            sha(patched) == PATCHED_SHA256, 'Unreviewed patched LK payload')
    originals = (original_a, original_b)
    profiles = tuple(profile(item) for item in originals)
    require(any(item in ('stock-v0.8.293', 'current-patched') for item in profiles),
            'At least one recognized LK prefix is required')
    images = tuple((name, patched + original[PAYLOAD_BYTES:])
                   for name, original in zip(SLOTS, originals))
    result = PreparedSlots(images, tuple((name, sha(original))
                           for name, original in zip(SLOTS, originals)),
                           tuple(zip(SLOTS, profiles)))
    validate(result, original_a, original_b, patched)
    return result


def validate(prepared, original_a, original_b, patched):
    """Recheck against actual supplied backup bytes; no boolean completion token."""
    require(type(prepared) is PreparedSlots, 'Unexpected prepared LK object')
    for rows in (prepared.originals, prepared.profiles):
        require(type(rows) is tuple and len(rows) == 2 and
                all(type(row) is tuple and len(row) == 2 and
                    type(row[0]) is str and type(row[1]) is str for row in rows),
                'Expected immutable LK binding rows with plain strings')
    require(type(patched) is bytes and len(patched) == PAYLOAD_BYTES and
            sha(patched) == PATCHED_SHA256, 'Unreviewed patched LK payload')
    originals = (original_a, original_b)
    profiles = tuple(profile(item) for item in originals)
    require(any(item in ('stock-v0.8.293', 'current-patched') for item in profiles),
            'At least one recognized LK prefix is required')
    require(prepared.originals == tuple((name, sha(original))
            for name, original in zip(SLOTS, originals)), 'Original LK binding differs')
    require(prepared.profiles == tuple(zip(SLOTS, profiles)), 'LK profile binding differs')
    require(type(prepared.images) is tuple and len(prepared.images) == 2,
            'Expected both prepared LK images')
    for row, name, original in zip(prepared.images, SLOTS, originals):
        require(type(row) is tuple and len(row) == 2 and type(row[0]) is str and row[0] == name and
                type(row[1]) is bytes and len(row[1]) == PARTITION_BYTES,
                'Prepared LK slot shape differs')
        require(row[1][:PAYLOAD_BYTES] == patched and
                row[1][PAYLOAD_BYTES:] == original[PAYLOAD_BYTES:],
                'Prepared LK prefix or original tail differs')
    return True
