"""Bounded, offline GPT pair validation for an r1 installer.

No transport, writes, recovery, or proof of physical read freshness. Callers
must acquire BOTH physical headers and arrays independently from the eMMC user
area. mtkclient's `gpt_backup.bin` does not satisfy that acquisition contract.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import re
import struct
import zlib

SECTOR = 512
MAX_ENTRIES = 4096
ENTRY_SIZE = 128
REQUIRED = ('super', 'boot_a', 'boot_b', 'dtbo_a', 'dtbo_b', 'vbmeta_a',
            'vbmeta_b', 'logo', 'userdata', 'md_udc', 'lk_a', 'lk_b',
            'para', 'boot_para')


class InvalidGPT(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InvalidGPT(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Header:
    current_lba: int
    backup_lba: int
    first_usable: int
    last_usable: int
    disk_guid: str
    array_lba: int
    count: int
    array_crc: int
    raw_sha256: str

    @property
    def array_bytes(self):
        return self.count * ENTRY_SIZE

    @property
    def read_bytes(self):
        return (self.array_bytes + SECTOR - 1) // SECTOR * SECTOR


@dataclass(frozen=True)
class Partition:
    name: str
    start: int
    length: int
    type_guid: str
    unique_guid: str
    attributes: int


@dataclass(frozen=True)
class Pair:
    user_bytes: int
    primary: Header
    backup: Header
    partitions: tuple[Partition, ...]
    primary_array_sha256: str
    backup_array_sha256: str

    def partition(self, name):
        for part in self.partitions:
            if part.name == name:
                return part
        raise InvalidGPT('missing partition: ' + name)

    def fingerprint(self):
        return sha(json.dumps(asdict(self), sort_keys=True,
                              separators=(',', ':')).encode())


def parse_header(raw, *, user_bytes, role, disk_guid_policy="nonzero"):
    """Validate a bounded sector before using its array address/read length.

    user_bytes must ultimately come from an independently checked live storage
    capacity, never from the untrusted GPT itself or stock scatter userdata size.
    This strict profile supports revision 1.0, 92-byte headers, 128-byte entries
    and 512-byte logical sectors; other formats require separate review.
    The default requires a nonzero disk GUID. Explicit "zero" requires zero;
    neither policy changes partition GUID checks or identifies hardware.
    """
    require(type(disk_guid_policy) is str and disk_guid_policy in ("nonzero", "zero"),
            "unsupported disk GUID policy")
    require(type(raw) is bytes and len(raw) == SECTOR, 'header sector size/type')
    require(type(user_bytes) is int and 68 * SECTOR <= user_bytes < 2**64 and
            user_bytes % SECTOR == 0, 'user capacity')
    require(role in ('primary', 'backup'), 'header role')
    require(raw[:8] == b'EFI PART', 'GPT signature')
    revision, size, crc, reserved = struct.unpack_from('<4I', raw, 8)
    require(revision == 0x10000 and size == 92 and reserved == 0 and
            not any(raw[size:]), 'unsupported header or reserved bytes')
    checked = bytearray(raw[:size])
    struct.pack_into('<I', checked, 16, 0)
    require(zlib.crc32(checked) == crc, 'header CRC')
    current, alternate, first, last = struct.unpack_from('<4Q', raw, 24)
    guid = raw[56:72]
    array, count, stride, array_crc = struct.unpack_from('<Q3I', raw, 72)
    end = user_bytes // SECTOR - 1
    expected = (1, end) if role == 'primary' else (end, 1)
    require((current, alternate) == expected, 'physical header locations')
    require(2 <= first <= last < end, 'usable bounds')
    require(bool(any(guid)) == (disk_guid_policy == "nonzero"),
            'disk GUID does not match explicit policy')
    require(1 <= count <= MAX_ENTRIES and stride == ENTRY_SIZE, 'entry count/stride')
    sectors = (count * stride + SECTOR - 1) // SECTOR
    if role == 'primary':
        require(2 <= array and array + sectors <= first, 'primary array bounds')
    else:
        require(last < array and array + sectors <= end, 'backup array bounds')
    return Header(current, alternate, first, last, guid.hex(), array,
                  count, array_crc, sha(raw))


def parse_pair(primary_header, primary_array, backup_header, backup_array, *,
               user_bytes, disk_guid_policy="nonzero"):
    p = parse_header(primary_header, user_bytes=user_bytes, role='primary',
                     disk_guid_policy=disk_guid_policy)
    b = parse_header(backup_header, user_bytes=user_bytes, role='backup',
                     disk_guid_policy=disk_guid_policy)
    require((p.first_usable, p.last_usable, p.disk_guid, p.count, p.array_crc) ==
            (b.first_usable, b.last_usable, b.disk_guid, b.count, b.array_crc),
            'headers disagree')
    for h, data in ((p, primary_array), (b, backup_array)):
        require(type(data) is bytes and len(data) == h.read_bytes, 'array size/type')
        require(not any(data[h.array_bytes:]), 'nonzero array sector padding')
        require(zlib.crc32(data[:h.array_bytes]) == h.array_crc, 'array CRC')
    require(primary_array == backup_array, 'partition arrays differ')
    partitions = []
    names, guids = set(), set()
    for offset in range(0, p.array_bytes, ENTRY_SIZE):
        entry = primary_array[offset:offset + ENTRY_SIZE]
        if not any(entry[:16]):
            require(not any(entry), 'nonzero unused entry')
            continue
        require(any(entry[16:32]), 'zero partition GUID')
        lo, hi, attributes = struct.unpack_from('<3Q', entry, 32)
        require(p.first_usable <= lo <= hi <= p.last_usable, 'partition bounds')
        try:
            name_field = entry[56:128].decode('utf-16-le', errors='strict')
        except UnicodeDecodeError as exc:
            raise InvalidGPT('invalid name encoding') from exc
        name, sep, tail = name_field.partition('\0')
        require(not sep or not any(ord(c) for c in tail), 'nonzero name padding')
        require(re.fullmatch(r'[A-Za-z0-9_.-]{1,36}', name) is not None, 'partition name')
        # mtkclient's named lookup is case-insensitive. Reject aliases even if
        # the GPT itself technically permits repeated or differently cased names.
        require(name.casefold() not in names and entry[16:32] not in guids,
                'duplicate name or partition GUID')
        names.add(name.casefold()); guids.add(entry[16:32])
        partitions.append(Partition(name, lo * SECTOR, (hi - lo + 1) * SECTOR,
                                    entry[:16].hex(), entry[16:32].hex(), attributes))
    ordered = sorted(partitions, key=lambda part: part.start)
    require(all(a.start + a.length <= b.start for a, b in zip(ordered, ordered[1:])),
            'overlapping partitions')
    return Pair(user_bytes, p, b, tuple(partitions), sha(primary_array), sha(backup_array))


def r1_regions(pair):
    """Select supported full-install ranges; no slot/preloader/security writes.

    Fixed payload sizes follow the supplied stock release. userdata's size is
    deliberately taken from this pair. This does not authenticate an r1 device.
    Nonzero target attributes are rejected rather than given guessed semantics.
    """
    require(type(pair) is Pair, 'pair type')
    expected = {'super': 8792064000, 'logo': 11 * 1024**2,
                'para': 512 * 1024, 'boot_para': 1024**2,
                'md_udc': 23699456}
    for base, length in [('boot', 32 * 1024**2), ('dtbo', 8 * 1024**2),
                         ('vbmeta', 8 * 1024**2), ('lk', 1024**2)]:
        expected.update({base + '_' + slot: length for slot in ('a', 'b')})
    regions = tuple(pair.partition(name) for name in REQUIRED)
    for part in regions:
        require(part.attributes == 0, 'unsupported r1 attributes: ' + part.name)
        if part.name in expected:
            require(part.length == expected[part.name], 'unsupported r1 size: ' + part.name)
    return regions
