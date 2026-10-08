#!/usr/bin/env python3
"""Check LK's console caller and successive Linux FDT updates with fixed inputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

from unicorn import UC_HOOK_CODE, UC_PROT_READ
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10)

ROOT = Path('/rabbitr1')


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load('platform_fixup', 'test-lk-platform-fixup.py')
validate = load('validate', 'validate-kernel.py')
# Use the package's actual FDT and ramdisk destinations. Other addresses belong
# only to the emulator's stack, boot-argument and initialized-state fixtures.
p.FDT = 0x47880000
BIAS, FDT, ARGS, STACK, STOP = p.BIAS, p.FDT, p.ARGS, p.STACK, p.STOP
RAMDISK = 0x51b00000
CMDLINE = BIAS+0xbd64c
PANEL = b'ili9883_boe_mipi_hd_lcm_drv\0'


class Emulator(p.Emulator):
    def __init__(self, image, dtb, cmdline):
        super().__init__(image, dtb)
        self.models = []
        self.bootargs()
        self.uc.mem_write(CMDLINE, cmdline+b'\0')
        self.w32(BIAS+0x98f78, CMDLINE+len(cmdline))

    def write(self, uc, access, address, size, value, _):
        # All other memory, including instructions, preloader arguments,
        # initramfs and hardware registers, must remain untouched.
        ranges = [(FDT, FDT+0x80000), (STACK-0x4000, STACK+0x2000),
                  (CMDLINE, CMDLINE+0x800), (BIAS+0x98e30, BIAS+0x98e34),
                  (BIAS+0x98f5c, BIAS+0x98f60), (BIAS+0x98f78, BIAS+0x98f7c),
                  (BIAS+0xbce44, BIAS+0xbce48), (BIAS+0xc8f24, BIAS+0xd0f28),
                  (ARGS+0x7300, ARGS+0x730c),
                  # The shipped firmware/android helper lowercases "MT6765"
                  # in place; this is a string literal, not executable code.
                  (BIAS+0x62c64, BIAS+0x62c6a)]
        assert any(a <= address and address+size <= b for a, b in ranges), (
            hex(address), hex(uc.reg_read(UC_ARM_REG_PC)-BIAS))

    def configure(self, ramdisk, table, log_enabled, log_port):
        uc = self.uc
        # Free extents complement the five controlled reservations. Split at
        # the two synthetic DRAM ranks, including addresses above 4 GiB.
        free = []
        for rank, (first, end) in enumerate(((0x40000000, 0xc0000000),
                                           (0xc0000000, 0x140000000))):
            cursor = first
            for _, base, size, _ in sorted(p.REGIONS, key=lambda r: r[1]):
                if base >= end or base+size <= first:
                    continue
                if cursor < base:
                    free.append((cursor, base-cursor, rank))
                cursor = base+size
            if cursor < end:
                free.append((cursor, end-cursor, rank))
        self.w32(ARGS+0x48, len(free))
        for i, values in enumerate(free):
            uc.mem_write(ARGS+0x50+24*i, struct.pack('<QQI', *values))
        self.free = free
        self.w32(ARGS+0xc, log_port)
        uc.mem_write(ARGS+0x14, bytes([log_enabled]))
        # Cached devinfo, initialized eMMC descriptor, and v2 boot-info state.
        uc.mem_write(BIAS+0x153b56, b'\1')
        for index, value in {6: 0x80002, 30: 0x24}.items():
            self.w32(BIAS+0x153b58+index*4, value)
        self.w32(BIAS+0x151ec0, ARGS+0x7000)
        self.w32(ARGS+0x7000, 1)
        self.w32(ARGS+0x7008, ARGS+0x7100)
        uc.mem_write(ARGS+0x710b, b'\1')
        self.w32(BIAS+0xd0f80, 1)
        self.w32(BIAS+0xd0f94, 2)
        # SRAM values are controlled inputs based on the older UART capture;
        # security/SoC register reads are also models, not live measurements.
        for address, size in ((0x10d000, 0x5000), (0x11c50000, 0x1000),
                              (0x08000000, 0x1000)):
            uc.mem_map(address, size)
        self.w32(0x08000000, 0x6765)
        for offset, value in ((0x59bc, 0x10ea00), (0x59c0, 0x540),
                              (0x59c4, 1), (0x59c8, 0x7c0)):
            self.w32(ARGS+offset, value)
        for offset, value in ((0, 0x61646472), (0x3c, 0x73697a65),
                              (0xc, 0x10f200), (0x10, 0x100)):
            self.w32(0x10f1c0+offset, value)
        for address, size in ((0x10d000, 0x5000), (0x11c50000, 0x1000),
                              (0x08000000, 0x1000)):
            uc.mem_protect(address, size, UC_PROT_READ)
        uc.mem_map(RAMDISK, 0x400000)
        assert len(ramdisk) < 0x400000
        uc.mem_write(RAMDISK, ramdisk)
        uc.mem_protect(RAMDISK, 0x400000, UC_PROT_READ)
        uc.mem_write(ARGS+0x7200, PANEL)
        uc.mem_write(BIAS+0xba220, struct.pack('<Q', 0x7efe0000))
        # The unchanged DTBO selector initializes its global index before the
        # early overlay and Linux handoff. Execute it instead of inventing an
        # uninitialized-index failure by starting halfway through boot.
        assert len(table) >= 64 and struct.unpack_from('>I', table, 0x10)[0] == 1
        uc.mem_write(ARGS+0x7400, table[:64])
        self.w32(STACK, ARGS+0x7308)
        for reg, value in ((UC_ARM_REG_R0, ARGS+0x7400),
                           (UC_ARM_REG_R1, struct.unpack_from('>I', table, 0x28)[0]),
                           (UC_ARM_REG_R2, ARGS+0x7300), (UC_ARM_REG_R3, ARGS+0x7304),
                           (UC_ARM_REG_SP, STACK), (UC_ARM_REG_LR, STOP | 1)):
            uc.reg_write(reg, value)
        uc.emu_start((BIAS+0x214ec) | 1, STOP, count=100000)
        assert uc.reg_read(UC_ARM_REG_PC) == STOP and uc.reg_read(UC_ARM_REG_R0) == 0
        assert self.u32(BIAS+0x98f5c) == self.u32(ARGS+0x7300) == 0
        assert self.u32(ARGS+0x7304) == struct.unpack_from('>I', table, 0x20)[0]
        assert self.u32(ARGS+0x7308) == struct.unpack_from('>I', table, 0x24)[0]
        # Do not intercept any FDT accessor/mutator, formatter or memory routine.
        for offset in (0x1590, 0x1334, 0x158c, 0x118d0, 0x191bc, 0x4a470,
                       0x226c0, 0x2aa78, 0x259a0, 0x2a5f0):
            uc.hook_add(UC_HOOK_CODE, self.model, begin=BIAS+offset, end=BIAS+offset)
        for offset in (0x4308, 0x477c, 0x30944, 0x26738, 0x4190,
                       0x4070, 0x48d8, 0x330b8, 0x2ce6c):
            uc.hook_add(UC_HOOK_CODE, self.code, begin=BIAS+offset, end=BIAS+offset)
        got = lambda offset: self.u32(BIAS+self.got+offset)
        self.w32(got(0x400), 0)  # NORMAL_BOOT
        for reg, value in ((UC_ARM_REG_R4, BIAS+self.got), (UC_ARM_REG_R8, FDT),
                           (UC_ARM_REG_R9, 0x40080000), (UC_ARM_REG_R10, STOP | 1),
                           (UC_ARM_REG_SP, STACK)):
            uc.reg_write(reg, value)
        for offset, value in ((0x18, RAMDISK), (0x1c, 0), (0x14, got(0x440)),
                              (0x24, got(0x51c)), (0x1258, len(ramdisk))):
            self.w32(STACK+offset, value)
        self.w32(got(0x440), 0x7800000)
        self.w32(got(0x51c), 1)

    def model(self, uc, address, size, _):
        offset = address-BIAS
        self.models.append(offset)
        if offset in (0x259a0, 0x2a5f0):
            raise AssertionError(f'LK fatal {offset:#x}, caller {uc.reg_read(UC_ARM_REG_LR)-BIAS:#x}')
        values = {0x1590: ARGS+0x7200, 0x1334: 5753, 0x158c: 1,
                  0x118d0: 0x420000, 0x4a470: 125, 0x226c0: 0, 0x2aa78: 0}
        if offset == 0x191bc:
            # Read-only seccfg accessor, unlocked fixture. No storage I/O.
            target = uc.reg_read(UC_ARM_REG_R0)
            assert STACK-0x4000 <= target <= STACK-4
            self.w32(target, 3)
            result = 0
        else:
            result = values[offset]
        uc.reg_write(UC_ARM_REG_R0, result)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))


def unpack(profile):
    blob = (ROOT/f'dist/mtkclient/boot-{profile}.img').read_bytes()
    assert blob[:8] == b'ANDROID!'
    ksize, kaddr, rsize, raddr, ssize, _, tags, page, version = struct.unpack_from('<9I', blob, 8)
    assert (kaddr, raddr, ssize, tags, page, version) == (0x40080000, RAMDISK, 0, FDT, 2048, 2)
    dsize, daddr = struct.unpack_from('<IQ', blob, 1648)
    assert daddr == FDT
    roff = page+(ksize+page-1)//page*page
    doff = roff+(rsize+page-1)//page*page
    table = blob[doff:doff+dsize]
    assert struct.unpack_from('>8I', table) == (0xd7b7ab1e, dsize, 32, 32, 1, 32, page, 0)
    size, offset, *ids = struct.unpack_from('>8I', table, 32)
    assert offset == 64 and size+offset == dsize and ids == [0]*6
    cmdline = blob[64:576].split(b'\0')[0]+blob[608:1632].split(b'\0')[0]
    return table[offset:], blob[roff:roff+rsize], cmdline


def check_console(stock, patched, inputs):
    cases = []
    for profile, (dtb, _, cmdline) in inputs.items():
        assert b'console=ttyS0,921600n8' in cmdline
        for enabled in (0, 1):
            for port, digit in ((0x11002000, b'0'), (0x11003000, b'1'),
                                (0x11004000, b'2'), (0x11005000, b'3'), (0, b'1')):
                for name, image in (('stock', stock), ('patched', patched)):
                    emu = Emulator(image, dtb, cmdline)
                    emu.w32(ARGS+0xc, port)
                    emu.uc.mem_write(ARGS+0x14, bytes([enabled]))
                    bootmode = emu.u32(BIAS+emu.got+0x400)
                    emu.w32(STACK+0x34, bootmode)
                    emu.w32(bootmode, 0)
                    before = bytes(emu.uc.mem_read(ARGS, 0x10000))
                    emu.uc.reg_write(UC_ARM_REG_SP, STACK)
                    emu.uc.reg_write(UC_ARM_REG_R4, BIAS+emu.got)
                    emu.uc.emu_start((BIAS+0x1cb64) | 1, BIAS+0x1cb74, count=100000)
                    assert emu.uc.reg_read(UC_ARM_REG_PC) == BIAS+0x1cb74
                    assert emu.uc.reg_read(UC_ARM_REG_SP) == STACK
                    assert emu.uc.reg_read(UC_ARM_REG_R4) == BIAS+emu.got
                    result = bytes(emu.uc.mem_read(CMDLINE, len(cmdline)+1))
                    expected = cmdline
                    if name == 'stock':
                        expected = expected.replace(b'ttyS0', b'ttyS'+(digit if enabled else b'1'), 1)
                    assert result == expected+b'\0', 'LK changed the selected Linux console'
                    assert bytes(emu.uc.mem_read(ARGS, 0x10000)) == before
                    assert emu.data() == emu.before
                    cases.append(dict(profile=profile, image=name, log_enabled=enabled,
                                      log_port=port, console=result.split(b'console=')[1].split()[0].decode()))
    return cases


def check_pipeline(image, profile, inputs, overlay):
    dtb, ramdisk, cmdline = inputs
    emu = Emulator(image, dtb, cmdline)
    emu.configure(ramdisk, overlay, int(profile == 'expdb'), 0x11005000)
    uc = emu.uc
    before = bytes(uc.mem_read(ARGS, 0x10000))
    # Start after decompression; stop before charging checks, secure-world and
    # cache/MMU shutdown. This is a contiguous part of boot_linux_fdt, not boot.
    uc.emu_start((BIAS+0x1bd6c) | 1, BIAS+0x1d4a4, count=60000000)
    assert emu.assertion is None, emu.assertion
    assert uc.reg_read(UC_ARM_REG_PC) == BIAS+0x1d4a4, hex(uc.reg_read(UC_ARM_REG_PC)-BIAS)
    assert uc.reg_read(UC_ARM_REG_SP) == STACK
    assert uc.reg_read(UC_ARM_REG_R8) == FDT
    assert uc.reg_read(UC_ARM_REG_R9) == 0x40080000
    assert bytes(uc.mem_read(ARGS, 0x10000)) == before
    assert bytes(uc.mem_read(RAMDISK, len(ramdisk))) == ramdisk
    data = emu.data()
    total = struct.unpack_from('>I', data, 4)[0]
    assert 0 < total < 0x80000
    nodes, original = validate.fdt_nodes(data), validate.fdt_nodes(dtb)
    chosen = nodes['/chosen']
    tokens = chosen['bootargs'].rstrip(b'\0').split()
    assert tokens[:len(cmdline.split())] == cmdline.split(), 'Final Linux command line was rewritten'
    assert [s for s in tokens if s.startswith(b'console=')] == [b'console=ttyS0,921600n8']
    assert chosen['linux,initrd-start'] == struct.pack('>I', RAMDISK)
    assert chosen['linux,initrd-end'] == struct.pack('>I', RAMDISK+len(ramdisk))
    assert chosen['ram_console'] == struct.pack('<4I', 0x10ea00, 0x540, 1, 0x7c0)
    assert chosen['log_store'] == struct.pack('<2I', 0x10f200, 0x100)
    assert nodes['/memory']['reg'] == struct.pack('>QQ', 0x40000000, 0x100000000)
    assert nodes['/memory']['mblock_info'] == before[0x48:0x5860]
    for i, (name, base, size, mapping) in enumerate(p.REGIONS, 1):
        node = nodes[f'/reserved-memory/mblock-{i}-{name}']
        assert node['reg'] == struct.pack('>QQ', base, size)
        assert ('no-map' in node) == (mapping == 0)
        assert ('reusable' in node) == (mapping == 2)
    assert nodes['/debug-kinfo']['memory-region'] == nodes['/reserved-memory/mblock-2-aee_debug_kinfo']['phandle']
    handles = [struct.unpack('>I', n['phandle'])[0] for n in nodes.values() if 'phandle' in n]
    assert len(handles) == len(set(handles)) and not {0, 0xffffffff}.intersection(handles)
    for node, props in original.items():
        expected = dict(props)
        if node == '/':
            expected.update(model=b'MT6765\0', **{'model-external-name': b'MT6765V/XBA\0',
                                                 'model-part-name': b'MT6765V/XBA\0'})
        elif node == p.SSPM:
            expected['status'] = b'okay\0'
        elif node == '/memory':
            assert all(nodes[node][k] == v for k, v in props.items() if k != 'reg')
            continue
        elif node == '/chosen':
            assert all(nodes[node][k] == v for k, v in props.items() if k != 'bootargs')
            continue
        assert nodes[node] == expected, node
    assert all(offset in emu.calls for offset in (0x30944, 0x4a40, 0x32f4c, 0x26738,
               0x4190, 0x4070, 0x48d8, 0x256b4, 0x330b8, 0x30bb4, 0x2ce6c))
    assert '%s:PASS\n' in emu.logs  # actual mblock_sanity_check, not a stub
    (ROOT/f'out/lk-linux-fdt-{profile}.dtb').write_bytes(data[:total])
    print(f'PASS: {profile} successive Linux FDT updates, selected console, initrd and reservations', flush=True)
    return dict(profile=profile, final_fdt_bytes=total, fdt_sha256=hashlib.sha256(data[:total]).hexdigest(),
                bootargs=chosen['bootargs'].rstrip(b'\0').decode(), free_extents=emu.free,
                calls=emu.calls, modeled_calls=emu.models, added_nodes=sorted(nodes.keys()-original.keys()))


def main():
    stock = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert hashlib.sha256(stock).hexdigest() == p.STOCK_SHA
    path = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT/'dist/mtkclient/lk.bin'
    assert path.is_relative_to(ROOT)
    image = path.read_bytes()
    assert len(image) == len(stock)
    inputs = {profile: unpack(profile) for profile in ('ram', 'expdb')}
    console = check_console(stock, image, inputs)
    print(f'PASS: {len(console)} console fixtures; stock UART rewrite reproduced', flush=True)
    overlay = (ROOT/'dist/mtkclient/dtbo.img').read_bytes()
    pipelines = [check_pipeline(image, profile, data, overlay) for profile, data in inputs.items()]
    report = dict(stock_lk_sha256=p.STOCK_SHA, patched_lk_sha256=hashlib.sha256(image).hexdigest(),
                  console_cases=console, pipelines=pipelines,
                  scope='Console caller and contiguous post-decompression Linux FDT path through fdt_pack',
                  modeled='Logging, mutexes, display queries, lock-state read, elapsed time, initialized firmware state, SRAM and read-only security/SoC registers',
                  limits='No whole boot, firmware allocations, live RAM map, DMA/secure handoff or peripheral validation')
    (ROOT/'out/lk-linux-fdt-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Whole boot, actual memory ownership and hardware remain untested.')


if __name__ == '__main__':
    main()
