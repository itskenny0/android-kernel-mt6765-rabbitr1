#!/usr/bin/env python3
"""Audit the shipped I2C FIFO setup instructions; no IRQ or physical transfers."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (
    UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X19,
    UC_ARM64_REG_X20, UC_ARM64_REG_X22, UC_ARM64_REG_X23, UC_ARM64_REG_X25,
    UC_ARM64_REG_X26, UC_ARM64_REG_X28, UC_ARM64_REG_X29, UC_ARM64_REG_X30,
)

ROOT = Path('/rabbitr1')
IMAGE_SHA256 = '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'


def emulate(raw, bank, op, length, auxiliary=0):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    bias = 0x1000000
    uc.mem_map(bias, (len(raw)+4095)&~4095)
    uc.mem_write(bias, raw)
    obj, comp, stack, mmio = 0x20000000, 0x20001000, 0x20008000, 0x30000000
    uc.mem_map(obj, 0x10000)
    uc.mem_map(mmio, 0x1000)
    def put(addr, fmt, *values): uc.mem_write(addr, struct.pack(fmt, *values))
    # Context fields and outer-frame locals recovered from the checksummed
    # __mt_i2c_transfer instructions. Enter after message selection/copying.
    put(obj+1248, '<Q', mmio)
    put(obj+1312, '<Q', obj+0x2000)
    put(obj+1380, '<I', 100000)
    put(obj+1384, '<I', 5)
    put(obj+1404, '<I', op)
    put(obj+1408, '<HH', length, length)
    put(obj+1424, '<HH', auxiliary, 0x34)
    put(obj+1444, '<I', bank)
    put(obj+1528, '<Q', comp)
    put(comp, '<12B', 2, 0, 0, 1, 1, 1, 1, 0, 2, 0, 1, 0)
    put(comp+12, '<H', 0x1801)
    for offset, target in [(40,1452), (48,1256), (56,1410), (64,1424), (88,1375)]:
        put(stack+offset, '<Q', obj+target)
    data = bytes(range(0x30, 0x30+length))
    uc.mem_write(obj+0x2000, data)
    uc.reg_write(UC_ARM64_REG_SP, stack)
    for reg, value in [(UC_ARM64_REG_X19,obj), (UC_ARM64_REG_X20,obj+1216),
                       (UC_ARM64_REG_X23,obj+1248), (UC_ARM64_REG_X25,obj+1448),
                       (UC_ARM64_REG_X26,obj+1456), (UC_ARM64_REG_X28,obj+1528),
                       (UC_ARM64_REG_X29,stack+0x90)]:
        uc.reg_write(reg, value)
    writes = []
    use_dma = length > 8 or auxiliary > 8
    end = 0xa1d4d4 if use_dma else 0xa1e938
    def code(uc, address, size, _):
        at = address-bias
        if at == 0x1f01c: # _mcount is outside the selected setup.
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        elif at == 0x45c3c8: # Fixed synthetic parent clock.
            uc.reg_write(UC_ARM64_REG_X0, 26000000)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0xa1d490 <= at < end or 0xa208b4 <= at < 0xa20c44, hex(at)
    def mem(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            writes.append((address-mmio, size, value))
        else:
            assert obj <= address < obj+0x1000 or stack-1024 <= address < stack+0xf0, hex(address)
    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_WRITE, mem)
    uc.emu_start(bias+0xa1d490, bias+end, count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC) == bias+end
    assert uc.reg_read(UC_ARM64_REG_SP) == stack
    assert uc.reg_read(UC_ARM64_REG_X22) == int(use_dma)
    if use_dma:
        assert not writes
        return
    # A whitelist makes additional register/bank writes fail this audit.
    expected = [(0x4c,2,386), (0x48,2,0x404), (0x10,2,0x3a if op == 3 else 0x28),
                (0x28,2,0x8001), (0x1c,2,10), (0x34,2,3),
                (0x20,2,0x1b), (0x2c,2,0x1a), (0x30,2,0),
                (0x04,2,0x69 if op == 2 else 0x68), (0x0c,2,0x1ff),
                (0x38,2,5 if bank else 1), (0x08,2,0x129 if bank else 0x12f),
                (0x14,2,length)]
    if op == 3: expected.append((0x44,2,auxiliary))
    expected.append((0x18,2,2 if op == 3 else 1))
    if op != 2: expected.extend((0,1,value) for value in data)
    expected.extend([(0x40,2,1), (0x24,2,1)])
    assert writes == [(bank+at,size,value) for at,size,value in expected], (bank,op,length,auxiliary,writes)


def main():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert len(raw) == 26019840 and hashlib.sha256(raw).hexdigest() == IMAGE_SHA256
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    nodes = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    common = nodes['/i2c_common']
    for key, value in {'dma_support':2, 'idvfs':1, 'set_dt_div':1,
                       'check_max_freq':1, 'set_ltiming':1, 'ver':2,
                       'cnt_constraint':1}.items():
        assert common[key] == bytes([value]), (key, common[key])
    assert common['ext_time_config'] == bytes.fromhex('1801')
    # These omitted vendor properties retain their zero-initialized defaults.
    assert not {'fifo_support', 'control_irq_sel', 'dma_ver', 'set_aed'} & common.keys()
    fifo = dma = 0
    for bank in (0, 0x100):
        for length in (1, 3, 5, 8, 9):
            for op in (1, 2):
                emulate(raw, bank, op, length)
                if length > 8: dma += 1
                else: fifo += 1
            for auxiliary in (1, 3, 5, 8, 9):
                emulate(raw, bank, 3, length, auxiliary)
                if length > 8 or auxiliary > 8: dma += 1
                else: fifo += 1
    evidence = {'stock_image_sha256':IMAGE_SHA256, 'fifo_setup_cases':fifo,
                'dma_selection_cases':dma,
                'scope':'selected stock instructions; modeled context and MMIO; no IRQ, RX or physical bus'}
    (ROOT/'out/i2c-stock-fifo.json').write_text(json.dumps(evidence, indent=2)+'\n')
    print(f'PASS: {fifo} stock FIFO setups, {dma} DMA selections, both banks; exact writes and START=1')
    print('No DMA engine, IRQ, FIFO reception, peripheral or physical bus was emulated.')


if __name__ == '__main__':
    main()
