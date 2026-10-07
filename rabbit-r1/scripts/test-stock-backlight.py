#!/usr/bin/env python3
"""Audit selected RabbitOS backlight setup instructions with modeled PMIC calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (
    UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X1,
    UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X12, UC_ARM64_REG_X19,
    UC_ARM64_REG_X20, UC_ARM64_REG_X21, UC_ARM64_REG_X30,
)

ROOT = Path('/rabbitr1')
IMAGE_SHA256 = '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'


def emulate(raw, vendor, revision, linear, external, pwm, seeded_boost=None):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    bias = 0x1000000
    uc.mem_map(bias, (len(raw)+4095)&~4095)
    uc.mem_write(bias, raw)
    obj, stack = 0x20000000, 0x20008000
    uc.mem_map(obj, 0x10000)
    pdev, pdata, chip, bled = obj, obj+0x1000, obj+0x2000, obj+0x3000
    data_at = bias+0x17a11e0
    def put(addr, fmt, *values): uc.mem_write(addr, struct.pack(fmt,*values))
    put(pdev+192, '<Q', pdata)
    put(chip+68, '<BB', revision, vendor)
    put(bled+40, '<QQ', chip, pdev+16)
    # Published AArch64 platform-data bitfields; stock r1 values except for
    # the mode/enable fixtures. Enter after allocation and property parsing.
    put(pdata, '<5B', int(external) | 15<<1 | int(linear)<<5 | 3<<6,
        2 | int(pwm)<<2 | 2<<3 | 1<<5 | 1<<7, 0, 3 | 1<<4, 0)
    put(pdata+8, '<I', 512)
    if seeded_boost is not None:
        # A synthetic inherited data byte makes the early-revision branch
        # observable. The default stock array already has both bits set.
        put(data_at+1, '<B', seeded_boost)
    uc.reg_write(UC_ARM64_REG_SP, stack)
    for reg,value in [(UC_ARM64_REG_X0,chip), (UC_ARM64_REG_X12,bias+0x17a1000),
                       (UC_ARM64_REG_X19,pdev+16), (UC_ARM64_REG_X20,pdev),
                       (UC_ARM64_REG_X21,bled)]:
        uc.reg_write(reg,value)
    requests = []
    def code(uc,address,size,_):
        at = address-bias
        if at == 0x8af614: # mt6370_pmu_reg_block_write transport is modeled.
            assert uc.reg_read(UC_ARM64_REG_X0) == chip
            assert uc.reg_read(UC_ARM64_REG_X1) == 0xa0
            assert uc.reg_read(UC_ARM64_REG_X2) == 12
            assert uc.reg_read(UC_ARM64_REG_X3) == data_at
            requests.append((0xa0,bytes(uc.mem_read(data_at,12))))
            uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        elif at == 0x8af368: # mt6370_pmu_reg_write
            assert uc.reg_read(UC_ARM64_REG_X0) == chip
            assert uc.reg_read(UC_ARM64_REG_X1) == 0xad
            requests.append((0xad,bytes([uc.reg_read(UC_ARM64_REG_X2)])))
            uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x8b13c0 <= at < 0x8b15d0, hex(at)
    def mem(uc,access,address,size,value,_):
        assert (data_at <= address and address+size <= data_at+12) or \
               (address == pdev+200 and size == 8), (hex(address),size)
    uc.hook_add(UC_HOOK_CODE,code)
    uc.hook_add(UC_HOOK_MEM_WRITE,mem)
    uc.emu_start(bias+0x8b13c0,bias+0x8b15d0,count=1000)
    assert uc.reg_read(UC_ARM64_REG_PC) == bias+0x8b15d0
    assert uc.reg_read(UC_ARM64_REG_SP) == stack
    en = (0xbe if external else 0x7e) & (255 if linear else ~2)
    boost = (0x89 if seeded_boost is None else seeded_boost) | 0x64
    if revision <= 1: boost |= 0x88
    expected = bytes([en,boost,0xb4 if pwm else 0x34,0x30,
                      0x38 if vendor in (0x90,0xb0) else 7,0x3f,0,8,0x8c,0x80,0xff,0])
    assert requests == [(0xa0,expected),(0xad,b'\0')], requests
    return expected


def main():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert len(raw) == 26019840 and hashlib.sha256(raw).hexdigest() == IMAGE_SHA256
    assert raw[0x17a11e0:0x17a11ec] == bytes.fromhex('42890000000000008c80ff00')
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    nodes = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    bled = nodes['/mt6370_pmu_dts/bled']
    values = {'chan_en':15, 'bl_ovp_level':3, 'bl_ocp_level':2,
              'pwm_fsample':2, 'pwm_deglitch':1, 'pwm_hys_en':1, 'pwm_hys':0,
              'pwm_avg_cycle':0, 'bled_ramptime':3, 'bled_flash_ramp':1,
              'max_bled_brightness':512, 'bled_curr_scale':0, 'pwm_lpf_coef':0}
    for key,value in values.items(): assert bled['mt,'+key] == struct.pack('>I',value)
    assert 'mt,map_linear' in bled and 'mt,use_pwm' in bled
    assert not {'mt,ext_en_pin','mt,pwm_lpf_en','mt,bled_curr_mode'} & bled.keys()
    cases = 0
    for vendor in (0x80,0xa0,0xe0,0xf0,0x90,0xb0):
        for linear in (False,True):
            for external in (False,True):
                for pwm in (False,True):
                    emulate(raw,vendor,2,linear,external,pwm)
                    cases += 1
    for revision in (0,1,2,15):
        emulate(raw,0xe0,revision,True,False,True,seeded_boost=1)
    board = emulate(raw,0xe0,2,True,False,True)
    result = {'stock_image_sha256':IMAGE_SHA256, 'setup_cases':cases,
              'synthetic_revision_cases':4, 'stock_dt_parameters':values,
              'mt6370_board_setup_a0_ab':board.hex(),
              'scope':'selected setup instructions; modeled context and PMIC transport; no hardware'}
    (ROOT/'out/stock-backlight-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(f'PASS: {cases} stock backlight setups; linear bit 1, PWM, channel and chip fixtures')
    print('PASS: early-revision shutdown-disable branch with synthetic register data')
    print('Selected stock instructions only; no physical brightness, protection or PMIC communication tested.')


if __name__ == '__main__':
    main()
