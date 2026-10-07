#!/usr/bin/env python3
"""Check the actual DM table builder and decode known pstore ring fixtures."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('decoder', ROOT/'scripts/decode-expdb.py')
decoder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decoder)

with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='expdb-test-') as temp:
    tmp = Path(temp)
    source = tmp/'test.c'
    source.write_text('''
#define EXPDB_TEST
#include "/rabbitr1/initramfs/expdb-map.c"
#include <assert.h>
int main(int argc, char **argv) {
    _Alignas(8) unsigned char buf[4096];
    assert(argc == 2);
    assert(partition_name("mmcblk0p3"));
    assert(partition_name("mmcblk12p103"));
    const char *bad[] = {"mmcblk0", "mmcblk0boot0", "sda1", "../mmcblk0p3", "mmcblk0p", "mmcblk0p3/x", ""};
    for (unsigned int i = 0; i < sizeof(bad)/sizeof(*bad); i++)
        assert(!partition_name(bad[i]));
    assert(has_line(argv[1], "PARTNAME=expdb\\n"));
    assert(!has_line(argv[1], "PARTNAME=expdb_b\\n"));
    linear_table(buf, makedev(179, 3));
    struct dm_ioctl *dm = (void *)buf;
    struct dm_target_spec *target = (void *)(buf + dm->data_start);
    assert(dm->target_count == 1 && !strcmp(dm->name, "r1-expdb"));
    assert(dm->data_start >= sizeof(*dm));
    assert(!strcmp(target->target_type, "linear"));
    assert(!strcmp((char *)(target + 1), "179:3 0"));
    assert(target->sector_start == 0 && target->length == 36864);
    assert((target->length * 512) + (2 * 1024 * 1024) == EXPDB_BYTES);
    assert(target->next % 8 == 0);
    assert(dm->data_start + target->next <= sizeof(buf));
    return 0;
}
''')
    uevent = tmp/'uevent'
    uevent.write_text('DEVNAME=mmcblk0p3\nPARTNAME=expdb\n')
    subprocess.run(['gcc', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                    '-o', str(tmp/'test'), str(source)], check=True)
    subprocess.run([str(tmp/'test'), str(uevent)], check=True)

def ring(data, length, start, sig=decoder.SIGNATURE ^ 2):
    return struct.pack('<Iii', sig, length, start) + data

assert decoder.decode_ring(ring(b'abc.....', 3, 3), 0, 20, 2) == b'abc'
assert decoder.decode_ring(ring(b'XYZdefgh', 8, 3), 0, 20, 2) == b'defghXYZ'
assert decoder.decode_ring(ring(b'12345678', 8, 8), 0, 20, 2) == b'12345678'
assert decoder.decode_ring(ring(b'........', 0, 0), 0, 20, 2) == b''
assert decoder.decode_ring(ring(b'........', 0, 0, sig=0), 0, 20, 2) is None
for length, start in [(9, 0), (-1, 0), (3, -1), (3, 4), (3, 2)]:
    try:
        decoder.decode_ring(ring(b'........', length, start), 0, 20, 2)
    except ValueError:
        pass
    else:
        raise AssertionError('Accepted corrupt ring')
try:
    decoder.decode_ring(b'', 0, 20, 2)
except ValueError:
    pass
else:
    raise AssertionError('Accepted truncated input')
print('PASS: target selection, bounded DM table, partial/wrapped/empty/corrupt pstore rings')
