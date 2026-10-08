#!/usr/bin/env python3
"""Execute LK platform and reservation fixups against the compiled mainline DT."""
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import struct
import subprocess
import sys
import tempfile

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6,
    UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)

ROOT = Path('/rabbitr1')
STOCK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
BIAS, FDT, ARGS, STACK, STOP = 0x47fffe00, 0x50000000, 0x51000000, 0x60008000, 0x70000000
SAVED = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
         UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]
SSPM = '/reserved-memory/reserve-memory-sspm_share'
# Controlled mblock v2 inputs, not a claim about current physical addresses.
# Include mapped/no-map/reusable entries and a range above the 32-bit boundary.
REGIONS = [('atf-reserved', 0x47d80000, 0x50000, 0),
           ('aee_debug_kinfo', 0x47c80000, 0x10000, 0),
           ('framebuffer', 0x7efe0000, 0x420000, 0),
           ('log_store', 0x7f7c0000, 0x40000, 1),
           ('test-high', 0x13fff0000, 0x10000, 2)]
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class Emulator:
    def __init__(self, image, dtb, loaded=0):
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        for at, size in [(BIAS & ~4095, 0x200000), (FDT, 0x80000),
                         (ARGS, 0x10000), (0x60000000, 0x10000), (STOP, 0x1000)]:
            uc.mem_map(at, size)
        uc.mem_write(BIAS, image)
        bss_start, bss_end = struct.unpack_from('<II', image, 0x32c)
        assert (bss_start, bss_end) == (0x480b7874, 0x4816a594)
        uc.mem_write(bss_start, bytes(bss_end-bss_start))
        data = bytearray(dtb.ljust(0x80000, b'\0'))
        assert len(data) == 0x80000
        struct.pack_into('>I', data, 4, len(data))
        self.before = bytes(data)
        uc.mem_write(FDT, self.before)
        self.w32(BIAS+0xbb724, loaded)  # state read by platform_fdt_scp()
        # Use the actual initialized GOT slot, not an assumed linked symbol.
        self.got = struct.unpack_from('<I', image, 0x1c828)[0]+0x1bc98
        slot = struct.unpack_from('<I', image, 0x1c88c)[0]
        assert self.got == 0xb745c and self.got+slot == 0xb7554
        assert self.u32(BIAS+self.got+slot) == (BIAS+0x4a40) | 1
        self.calls, self.logs, self.assertion, self.writes = [], [], None, 0
        # Restrict instruction hooks to entries; all shipped libfdt, libc and
        # formatting instructions still execute, without a slow per-insn hook.
        entries = (0x4a40, 0x14be8, 0x15c48, 0x49d50, 0x136e0, 0x73b0,
                   0x32f4c, 0x256b4, 0x30bb4, 0x2a3bc, 0x2a510,
                   0x18aa4, 0x19b78, 0x19c34)
        for at in entries:
            uc.hook_add(UC_HOOK_CODE, self.code, begin=BIAS+at, end=BIAS+at)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def u32(self, at):
        return struct.unpack('<I', self.uc.mem_read(at, 4))[0]

    def w32(self, at, value):
        self.uc.mem_write(at, struct.pack('<I', value))

    def text(self, at):
        return bytes(self.uc.mem_read(at, 256)).split(b'\0')[0].decode('ascii')

    def code(self, uc, address, size, _):
        at = address-BIAS
        if at == 0x2a510:
            self.assertion = {'return_offset': uc.reg_read(UC_ARM_REG_LR)-BIAS,
                              'format': self.text(uc.reg_read(UC_ARM_REG_R1))}
            uc.emu_stop()
        elif at in (0x2a3bc, 0x18aa4, 0x19b78, 0x19c34):
            # Logging/verbosity and uncontended mutex operations only. These
            # tests do not simulate other LK threads or physical MMIO.
            if at == 0x2a3bc:
                self.logs.append(self.text(uc.reg_read(UC_ARM_REG_R0)))
            uc.reg_write(UC_ARM_REG_R0, 0)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        else:
            self.calls.append(at)

    def write(self, uc, access, address, size, value, _):
        if FDT <= address and address+size <= FDT+0x80000:
            self.writes += 1
        else:
            # Stack, the default mrdump trigger string pointer, and the
            # debug-kinfo reservation registration table only.
            # Code, boot arguments and device MMIO may not be written.
            # The caller's assertion branch places a fifth argument at SP.
            assert (STACK-0x2000 <= address and address+size <= STACK+4 or
                    address == BIAS+0xbce44 and size == 4 or
                    BIAS+0xc8f24 <= address and address+size <= BIAS+0xd0f28), hex(address)

    def run(self, start, stop=STOP):
        uc = self.uc
        saved = {reg: 0xa000+i for i, reg in enumerate(SAVED)}
        saved.update({UC_ARM_REG_R4: BIAS+self.got, UC_ARM_REG_R8: FDT})
        for reg, value in saved.items():
            uc.reg_write(reg, value)
        uc.reg_write(UC_ARM_REG_R0, FDT)
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start((BIAS+start) | 1, stop, count=40000000)
        if self.assertion is None:
            assert uc.reg_read(UC_ARM_REG_PC) == stop, hex(uc.reg_read(UC_ARM_REG_PC)-BIAS)
            assert uc.reg_read(UC_ARM_REG_SP) == STACK
            for reg, value in saved.items():
                assert uc.reg_read(reg) == value
        return uc.reg_read(UC_ARM_REG_R0)

    def data(self):
        return bytes(self.uc.mem_read(FDT, 0x80000))

    def bootargs(self):
        # Boot argument pointer from the shipped GOT. These are fixture inputs
        # for the later functions, not a substitute for capturing a live FDT.
        self.w32(self.u32(BIAS+0xb78cc), ARGS)
        self.w32(ARGS+0x48, 1)
        self.uc.mem_write(ARGS+0x50, struct.pack('<QQI', 0x40000000, 0x100000000, 0))
        self.w32(ARGS+0xc50, 0x99999999)
        self.w32(ARGS+0xc54, 2)
        self.w32(ARGS+0xc58, len(REGIONS))
        for i, (name, base, size, mapping) in enumerate(REGIONS):
            self.uc.mem_write(ARGS+0xc60+0x98*i,
                struct.pack('<QQI', base, size, mapping)+name.encode()+b'\0')
        self.w32(ARGS+0x5860, 2)
        self.uc.mem_write(ARGS+0x5868,
            struct.pack('<QQQQ', 0x40000000, 0x80000000, 0xc0000000, 0x80000000))


def main():
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    stock = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert hashlib.sha256(stock).hexdigest() == STOCK_SHA
    candidate = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT/'dist/lk/lk.bin'
    assert candidate.is_relative_to(ROOT)
    patched = candidate.read_bytes()
    assert len(stock) == len(patched)
    base = (ROOT/'dist/mainline/mt6765-rabbit-r1.dtb').read_bytes()
    cases = []
    with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='lk-platform-') as tmp:
        dtb = Path(tmp)/'board.dtb'
        for profile, status in (('ram', 'disabled'), ('expdb', 'okay')):
            dtb.write_bytes(base)
            subprocess.run(['fdtput', '-t', 's', str(dtb), '/soc/mmc@11230000', 'status', status], check=True)
            data = dtb.read_bytes()
            original = validate.fdt_nodes(data)
            assert not any(b'mediatek,scp' in p.get('compatible', b'').split(b'\0')
                           for p in original.values())
            for loaded in (0, 1):
                for name, image in (('stock', stock), ('patched', patched)):
                    emu = Emulator(image, data, loaded)
                    emu.run(0x1beb4, BIAS+0x1bec8)
                    if name == 'stock':
                        assert emu.assertion == {'return_offset': 0x1c263,
                                                 'format': 'ASSERT at (%s:%d): %s\n'}
                        assert emu.calls == [0x4a40, 0x14be8]
                        assert emu.data() == emu.before and emu.writes == 0
                    else:
                        assert emu.assertion is None, 'Patched LK still aborts on the missing vendor SCP node'
                        assert emu.calls == [0x4a40, 0x15c48, 0x49d50, 0x136e0, 0x73b0]
                        expected = {k: dict(v) for k, v in original.items()}
                        expected[SSPM]['status'] = b'okay\0'
                        assert validate.fdt_nodes(emu.data()) == expected
                    cases.append({'profile': profile, 'image': name, 'scp_loaded': loaded,
                                  'calls': emu.calls, 'assertion': emu.assertion})
            # Keep the normal memory/debug-reservation pipeline, with actual
            # FDT/libc instructions and a controlled boot-argument fixture.
            emu = Emulator(patched, data)
            emu.bootargs()
            args_before = bytes(emu.uc.mem_read(ARGS, 0x10000))
            assert emu.run(0x1beb4, BIAS+0x1bec8) == 0 and emu.assertion is None
            for function in (0x32f4c, 0x256b4, 0x30bb4):
                assert emu.run(function) == 0 and emu.assertion is None
            nodes = validate.fdt_nodes(emu.data())
            assert bytes(emu.uc.mem_read(ARGS, 0x10000)) == args_before
            for i, (name, address, size, mapping) in enumerate(REGIONS, 1):
                node = nodes[f'/reserved-memory/mblock-{i}-{name}']
                assert node['reg'] == struct.pack('>QQ', address, size)
                assert node['compatible'] == f'mediatek,{name}\0'.encode()
                assert ('no-map' in node) == (mapping == 0)
                assert ('reusable' in node) == (mapping == 2)
            debug = nodes['/reserved-memory/mblock-2-aee_debug_kinfo']['phandle']
            assert nodes['/debug-kinfo']['memory-region'] == debug
            handles = [struct.unpack('>I', p['phandle'])[0] for p in nodes.values() if 'phandle' in p]
            assert len(handles) == len(set(handles)) and 0 not in handles and 0xffffffff not in handles
            assert nodes['/memory']['mblock_info'] == args_before[0x48:0x5860]
            # Only memory/chosen additions and SSPM status may affect existing
            # nodes. In particular keep all hardware status/pin/clock bindings.
            expected = {k: dict(v) for k, v in original.items()}
            expected[SSPM]['status'] = b'okay\0'
            for node, props in expected.items():
                if node in ('/memory', '/chosen'):
                    assert all(nodes[node][k] == v for k, v in props.items())
                else:
                    assert nodes[node] == props
            cases.append({'profile': profile, 'image': 'patched', 'fixture_reservations': len(REGIONS),
                          'calls': emu.calls, 'assertion': emu.assertion})
        # The unmodified SCP helper still works with a vendor-style node.
        subprocess.run(['fdtput', '-c', str(dtb), '/scp'], check=True)
        subprocess.run(['fdtput', '-t', 's', str(dtb), '/scp', 'compatible', 'mediatek,scp'], check=True)
        for loaded in (0, 1):
            emu = Emulator(patched, dtb.read_bytes(), loaded)
            assert emu.run(0x14be8) == 0 and emu.assertion is None
            assert validate.fdt_nodes(emu.data())['/scp']['status'] == (b'okay\0' if loaded else b'disabled\0')
            cases.append({'image': 'patched', 'vendor_scp_control': True, 'scp_loaded': loaded})
    report = {'stock_lk_sha256': STOCK_SHA, 'patched_lk_sha256': hashlib.sha256(patched).hexdigest(),
              'cases': cases, 'patch_offset': 0x4a44,
              'scope': 'Linux platform caller, fixups and selected later reservation functions with shipped libfdt/libc',
              'modeled': 'logging, verbosity, uncontended mutexes; synthetic boot arguments and SCP-loaded state',
              'limits': 'no complete LK boot, live reservation map or peripheral/remote-processor validation'}
    (ROOT/'out/lk-platform-fixup-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {len(cases)} platform/reservation fixtures; stock missing-SCP boot assertion reproduced')
    print('PASS: patched caller runs remaining fixups and preserves RAM/expdb hardware bindings')
    print('PASS: selected later memory/debug fixups preserve five supplied reservations, including >4 GiB')
    print('Whole boot and actual firmware memory ownership remain untested.')


if __name__ == '__main__':
    main()
