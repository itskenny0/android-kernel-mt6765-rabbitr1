#!/usr/bin/env python3
"""Offline artifact checks. These cannot establish that an r1 boots."""
import gzip
from pathlib import Path
import re
import struct
import sys

ROOT = Path('/rabbitr1')

def fdt_nodes(blob):
    if len(blob) < 40:
        raise ValueError('Truncated FDT header')
    magic, total, off_struct, off_strings, _, version, _, _, size_strings, size_struct = struct.unpack_from('>10I', blob)
    if magic != 0xd00dfeed or total > len(blob) or version < 17:
        raise ValueError('Invalid FDT header')
    if off_strings + size_strings > total or off_struct + size_struct > total:
        raise ValueError('FDT section outside blob')
    strings = blob[off_strings:off_strings+size_strings]
    pos, end, stack, nodes = off_struct, off_struct+size_struct, [], {}
    while pos + 4 <= end:
        token = struct.unpack_from('>I', blob, pos)[0]
        pos += 4
        if token == 1:
            nul = blob.index(0, pos, end)
            stack.append(blob[pos:nul].decode())
            pos = (nul + 4) & ~3
            nodes['/'.join(stack) or '/'] = {}
        elif token == 2:
            if not stack:
                raise ValueError('Unbalanced FDT')
            stack.pop()
        elif token == 3:
            length, offset = struct.unpack_from('>II', blob, pos)
            pos += 8
            if not stack or pos+length > end or offset >= len(strings):
                raise ValueError('Invalid FDT property')
            name = strings[offset:strings.index(0, offset)].decode()
            nodes['/'.join(stack) or '/'][name] = blob[pos:pos+length]
            pos = (pos+length+3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            if stack:
                raise ValueError('Unclosed FDT node')
            return nodes
        else:
            raise ValueError(f'Unknown FDT token {token}')
    raise ValueError('Missing FDT end')

def check_mainline(dtb):
    nodes = fdt_nodes(dtb.read_bytes())
    assert nodes['/']['compatible'].split(b'\0')[:2] == [b'rabbit,r1', b'mediatek,mt6765']
    memory = {name for name, props in nodes.items() if props.get('device_type') == b'memory\0'}
    assert memory == {'/memory@40000000'}, 'Expected one unit-addressed LK memory fixup target'
    clocks = ROOT/'src/mainline/include/dt-bindings/clock/mt6765-clk.h'
    ids = dict(re.findall(r'^#define\s+(CLK_\w+)\s+(\d+)\s*$', clocks.read_text(), re.M))
    provider = nodes['/soc/clock-controller@10000000']['phandle']
    for addr, name in [('11230000','CLK_TOP_MSDC50_0'), ('11240000','CLK_TOP_MSDC30_1')]:
        clocks_prop = nodes['/soc/mmc@'+addr]['clocks']
        assert clocks_prop[:4] == provider, f'MMC {addr}: source clock references wrong provider'
        assert struct.unpack('>I',clocks_prop[4:8])[0] == int(ids[name])
    assert nodes['/soc/mmc@11230000']['status'] == b'disabled\0', 'Default eMMC bring-up gate lost'
    assert nodes['/soc/usb@11200000']['dr_mode'] == b'peripheral\0'
    secure_ids = {'11009000': 2, '1100f000': 3, '11011000': 4, '1100d000': 6}
    for addr in ['11007000', '11008000', '11009000', '1100f000', '11011000', '11016000', '1100d000']:
        assert nodes['/soc/i2c@'+addr]['compatible'] == b'mediatek,mt6765-i2c\0', \
            f'I2C {addr}: MT6765 interrupt handling requires its own compatible'
        assert nodes['/soc/i2c@'+addr]['clock-div'] == struct.pack('>I', 5), \
            f'I2C {addr}: unexpected programmable divider'
        if addr in secure_ids:
            assert nodes['/soc/i2c@'+addr]['mediatek,secure-id'] == struct.pack('>I', secure_ids[addr])
        else:
            assert 'mediatek,secure-id' not in nodes['/soc/i2c@'+addr]
    print('PASS: r1 identity, LK memory path, MMC clock providers/IDs, eMMC gate, USB role, I2C match/dividers')

def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--dtb':
        dtb = Path(sys.argv[2]).resolve()
        if not dtb.is_relative_to(ROOT):
            raise SystemExit('Input must be under /rabbitr1')
        check_mainline(dtb)
        return
    target = sys.argv[1] if len(sys.argv) == 2 else 'mainline'
    if target not in ('mainline','vendor'):
        raise SystemExit('Expected mainline or vendor')
    dist = ROOT/'dist'/target
    image = gzip.decompress((dist/'Image.gz').read_bytes())
    assert image == (dist/'Image').read_bytes(), 'Compressed Image mismatch'
    assert image[56:60] == b'ARM\x64', 'Not an AArch64 Linux Image'
    elf = (dist/'vmlinux').read_bytes()[:64]
    assert elf[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H',elf,18)[0] == 183
    print(f'PASS: {target} gzip, AArch64 Image header, AArch64 ELF')
    if target == 'mainline':
        check_mainline(dist/'mt6765-rabbit-r1.dtb')
        config = (dist/'config').read_text()
        for key in ['ANDROID_BINDER_IPC','ANDROID_BINDERFS','SECURITY_SELINUX','BPF_SYSCALL','CGROUP_BPF',
                    'USB_CONFIGFS_F_FS','USB_CONFIGFS_ACM','BLK_DEV_INITRD','DEVTMPFS',
                    'BLK_DEV_DM','PSTORE','PSTORE_CONSOLE','PSTORE_PMSG','MMC_MTK','EFI_PARTITION']:
            assert f'CONFIG_{key}=y\n' in config, f'Missing CONFIG_{key}'
        for key in ['MFD_MT6370', 'MEDIATEK_MT6370_ADC', 'SENSORS_IIO_HWMON']:
            assert f'CONFIG_{key}=y\n' in config, f'Missing power monitor CONFIG_{key}'
        assert 'CONFIG_DRM_PANEL_RABBIT_R1=y\n' in config, 'Missing r1 panel driver'
        for key in ['USB_ETH','USB_G_HID','CPU_FREQ','CPU_IDLE','SUSPEND','HIBERNATION']:
            assert f'CONFIG_{key}=y\n' not in config and f'CONFIG_{key}=m\n' not in config, f'Unexpected CONFIG_{key}'
        for key in ['PSTORE_ZONE', 'PSTORE_BLK']:
            assert f'CONFIG_{key}=m\n' in config, f'Missing logging module CONFIG_{key}'
        print('PASS: required bring-up / Android groundwork configuration')

if __name__ == '__main__':
    main()
