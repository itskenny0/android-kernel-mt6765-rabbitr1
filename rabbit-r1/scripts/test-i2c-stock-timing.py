#!/usr/bin/env python3
"""Compare mainline timing with RabbitOS instructions; no real MMIO or transfers."""
from pathlib import Path
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (
    UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X19,
    UC_ARM64_REG_X22, UC_ARM64_REG_X23, UC_ARM64_REG_X24, UC_ARM64_REG_X25,
    UC_ARM64_REG_X28, UC_ARM64_REG_X30,
)
ROOT = Path('/rabbitr1')
IMAGE_SHA256 = '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
# Kallsyms in the checksummed Image identifies the helper at 0xa208b4 and
# calculate_speed at 0xa20ab0. These are file offsets, not physical addresses.
# The selected transfer fragment is 0xa1d538..0xa1db78. It calls the real
# helpers, programs timing/control registers, and stops before address/DMA/START.
REGISTERS = (0x20, 0x2c, 0x30, 0x48, 0x4c, 0x28, 0x1c, 0x34)
def emulate(raw,parent,speed,divider,bank):
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    bias=0x1000000 # Synthetic mapping preserves page-relative instructions.
    uc.mem_map(bias,(len(raw)+4095)&~4095)
    uc.mem_write(bias,raw)
    obj,comp,stack,mmio=0x20000000,0x20001000,0x20008000,0x30000000
    uc.mem_map(obj,0x10000)
    uc.mem_map(mmio,0x1000)
    def put(addr,fmt,*values): uc.mem_write(addr,struct.pack(fmt,*values))
    # Field offsets come from the shipped AArch64 instructions. Compatibility
    # bytes match the verified stock DT: set_dt_div, check_max_freq, set_ltiming,
    # version=2, cnt_constraint=1, ext_time_config=0x1801. hs_only remains zero,
    # so high-speed fixtures include the standard master-code phase.
    put(obj+1248,'<Q',mmio)
    put(obj+1380,'<I',speed)
    put(obj+1384,'<I',divider)
    put(obj+1404,'<I',1)
    put(obj+1448,'<I',bank)
    put(obj+1528,'<Q',comp)
    put(comp+4,'<5B',1,1,1,0,2)
    put(comp+10,'<B',1)
    put(comp+12,'<H',0x1801)
    uc.reg_write(UC_ARM64_REG_SP,stack)
    for r,v in [(UC_ARM64_REG_X19,obj),(UC_ARM64_REG_X22,1),(UC_ARM64_REG_X23,obj+1248),(UC_ARM64_REG_X24,speed),(UC_ARM64_REG_X25,obj+1448),(UC_ARM64_REG_X28,obj+1528)]: uc.reg_write(r,v)
    writes=[]
    def code(uc,address,size,_):
        at=address-bias
        if at==0x1f01c: # _mcount: tracing is outside this timing test.
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        elif at==0x45c3c8: # clk_get_rate: supply the clock-rate fixture.
            uc.reg_write(UC_ARM64_REG_X0,parent)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        elif at==0xa1f1f8: uc.emu_stop()
        else:
            assert 0xa1d538<=at<0xa1db78 or 0xa208b4<=at<0xa20c44,hex(at)
    def mem(uc,access,address,size,value,_):
        if mmio<=address<mmio+0x1000: writes.append((address-mmio,size,value))
        else: assert obj<=address<obj+0x1000 or stack-1024<=address<stack,(hex(address),size)
    uc.hook_add(UC_HOOK_CODE,code)
    uc.hook_add(UC_HOOK_MEM_WRITE,mem)
    uc.emu_start(bias+0xa1d538,bias+0xa1db78,count=10000)
    pc = uc.reg_read(UC_ARM64_REG_PC) - bias
    assert pc in (0xa1db78, 0xa1f1f8), hex(pc)
    assert uc.reg_read(UC_ARM64_REG_SP) == stack
    if pc == 0xa1f1f8:
        assert not writes, 'Invalid timing must fail before programming registers'
        return None
    offsets = [address-bank for address, size, value in writes]
    # No access to CCU or another transaction bank, and no hidden MMIO writes.
    expected = [0x4c,0x48,0x10,0x28,0x1c,0x34,0x20,0x2c,0x30]
    if not any(address == bank+0x4c for address, size, value in writes):
        expected.remove(0x4c) # Stock skips timeout programming when LTIMING is zero.
    assert offsets == expected and all(size == 2 for address,size,value in writes), writes
    return {address-bank:value for address,size,value in writes}


def main():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert len(raw) == 26019840 and hashlib.sha256(raw).hexdigest() == IMAGE_SHA256
    spec = importlib.util.spec_from_file_location('validate',ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    nodes = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    common = nodes['/i2c_common']
    for key,value in {'set_dt_div':1,'check_max_freq':1,'set_ltiming':1,
                      'ver':2,'cnt_constraint':1}.items():
        assert common[key] == bytes([value]), (key,common[key])
    assert common['ext_time_config'] == bytes.fromhex('1801')
    assert nodes['/i2c4@11011000']['clock-div'] == struct.pack('>I',5)
    assert nodes['/i2c5@11016000']['clock-div'] == struct.pack('>I',5)
    # Compile the current production functions, never reuse a stale harness.
    subprocess.run([sys.executable,str(ROOT/'scripts/test-i2c.py')],check=True)
    binary = ROOT/'out/i2c-tests/timing'
    evidence = []
    matched = rejected = 0
    for parent in (26000000,65000000,104000000,124800000,136500000):
        for speed in (100000,400000,1000000,1700000,3400000):
            for divider in (1,2,5,8):
                for bank in (0,0x100):
                    stock = emulate(raw,parent,speed,divider,bank)
                    actual = list(map(int,subprocess.check_output(
                        [str(binary),str(parent),str(speed),str(divider),str(bank)],text=True).split()))
                    case = (parent,speed,divider,bank)
                    if stock is None:
                        assert actual[0] < 0, (case,actual)
                        rejected += 1
                        continue
                    expected = [0]+[stock.get(reg,0) for reg in REGISTERS]
                    assert actual == expected, (case,actual,expected)
                    matched += 1
                    evidence.append({'parent_hz':parent,'speed_hz':speed,'divider':divider,
                                     'bank':bank,'registers':{hex(reg):stock[reg] for reg in REGISTERS}})
    # A representability boundary in the vendor calculation: sample=1, step=64
    # becomes zero when masked into a six-bit field. Mainline uses sample=2,
    # step=32 for the same product, and programs the timeout rather than skipping it.
    stock = emulate(raw,64000000,100000,5,0)
    assert stock[0x20] == 1 and stock[0x2c] == 0 and 0x4c not in stock
    actual = list(map(int,subprocess.check_output(
        [str(binary),'64000000','100000','5','0'],text=True).split()))
    assert actual == [0,0x11f,0x5f,0,0x404,400,0x8001,10,3], actual
    result = {'stock_image_sha256':IMAGE_SHA256,'matching_cases':matched,
              'rejected_cases':rejected,'count_boundary_difference_verified':True,
              'scope':'selected stock setup fragment with modeled clocks/MMIO; no bus activity',
              'cases':evidence}
    (ROOT/'out/i2c-stock-timing.json').write_text(json.dumps(result,indent=2)+'\n')
    print(f'PASS: {matched} register sets match stock instructions; {rejected} unsupported rates rejected')
    print('PASS: 64-step boundary uses a representable sample/step pair and configures timeout')
    print('No IRQ, DMA, START, physical clock or electrical timing was emulated or measured.')


if __name__ == '__main__':
    main()
