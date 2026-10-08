#!/usr/bin/env python3
"""Compare MT6765 DSI sleep, wake, reset and mode writes with shipped code."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import *

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('command', Path(__file__).with_name('test-dsi-command.py'))
command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(command)


def initial(seed, enter):
    return {0:seed & ~1, 8:seed & ~0x8040, 0xc:0, 0x10:seed & ~5,
            0x14:seed, 0x18:seed, 0xa0:seed,
            0x104:(seed & ~2) if enter else seed|2,
            0x108:(seed & ~2) if enter else seed|2}


def stock(raw, operation, seed, rate=260, lanes=2, wait='early', mode=0):
    bias, obj, mmio, stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000);uc.mem_map(mmio,0x1000);uc.mem_map(stop,0x1000)
    def save(address, value, size=4): uc.mem_write(address,value.to_bytes(size,'little'))
    def load(address, size=4): return int.from_bytes(uc.mem_read(address,size),'little')
    enter = operation in ('enter','off')
    regs = initial(seed,enter)
    for off,val in regs.items(): save(mmio+off,val)
    save(bias+0x1a06960,mmio,8)
    save(bias+0x1a07f44,lanes)
    save(bias+0x1a07ff4,rate//2)
    save(bias+0x1a07ffc,rate)
    save(bias+0x1a08f18,0)
    save(bias+0x1a06758,0)
    save(bias+0x1a08230,int(enter))
    save(bias+0x1a38bf8,0)
    save(bias+0x18730d2,0,1)
    save(bias+0x1a08310,0);save(bias+0x1a08330,0)
    functions = {'enter':0x70fc2c,'exit':0x7101f8,'on':0x72466c,'off':0x7249a4,
                 'reset':0x70ff84,'mode':0x721174}
    allowed = [(0x70f9a8,0x710658),(0x714444,0x7144f8),(0x721174,0x721330),
               (0x72466c,0x724b38)]
    uc.reg_write(UC_ARM64_REG_X0,13);uc.reg_write(UC_ARM64_REG_X1,0)
    uc.reg_write(UC_ARM64_REG_X2,mode)
    uc.reg_write(UC_ARM64_REG_SP,obj+0x8000);uc.reg_write(UC_ARM64_REG_X30,stop)
    saved = list(range(UC_ARM64_REG_X19,UC_ARM64_REG_X28+1))+[UC_ARM64_REG_X29]
    for n,reg in enumerate(saved): uc.reg_write(reg,0x12340000+n)
    writes, calls = [],[]
    completion = None
    schedules = 0
    def complete():
        assert completion is not None
        save(bias+(0x1a08330 if enter else 0x1a08310),1)
        if not enter:
            save(mmio+0x104,load(mmio+0x104)&~2)
            save(mmio+0x108,load(mmio+0x108)&~2)
    def code(uc,address,size,_):
        nonlocal schedules
        at=address-bias
        if at in (0x1f01c,0x747728,0xa5e2c,0x420ad0,0x70ec88,
                  0xf1dee0,0x8c2d0,0x8c314,0x8c5ac,0xf36ad0,
                  0x72a40c,0x715cb0,0x72a0a0,0x72a210,0x74398c):
            if at == 0xf1dee0:
                assert uc.reg_read(UC_ARM64_REG_X0)==0x10c7
                writes.append([0xffff,1])
            elif at == 0xf36ad0:
                schedules+=1
                assert completion is not None
                if wait=='late': complete()
                uc.reg_write(UC_ARM64_REG_X0,499 if wait=='late' else 0)
            elif at in (0x72a40c,0x715cb0,0x72a0a0,0x72a210):
                calls.append([hex(at),*[uc.reg_read(reg) for reg in
                    (UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2)]])
                uc.reg_write(UC_ARM64_REG_X0,0)
            elif at == 0x74398c: uc.reg_write(UC_ARM64_REG_X0,obj)
            elif at not in (0x1f01c,0xf1dee0): uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert any(start<=at<end for start,end in allowed),hex(at)
    def write(uc,access,address,size,value,_):
        nonlocal completion
        if mmio<=address<mmio+0x1000:
            assert size==4
            off=address-mmio;writes.append([off,value])
            if (enter and off==0x108 and value&2) or (not enter and off==0 and value&4):
                completion=True
                if wait=='early': complete()
        else:
            assert (obj+0x7000<=address and address+size<=obj+0x8000 or
                    bias+0x1a07d90<=address and address+size<=bias+0x1a08940),hex(address)
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+functions[operation],stop,count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert [uc.reg_read(reg) for reg in saved]==[0x12340000+n for n in range(len(saved))]
    if operation in ('on','off'):
        assert uc.reg_read(UC_ARM64_REG_X0)==0
        assert load(bias+0x1a08230)==int(operation=='on')
    if operation in ('enter','exit','on','off'):
        assert schedules==(wait!='early')
    return dict(operation=operation,seed=seed,rate=rate,lanes=lanes,wait=wait,mode=mode,
                writes=writes,calls=calls,registers={hex(off):load(mmio+off) for off in regs})


EXTRA = r'''
static void power_failures(void)
{
    const u32 seeds[]={0,2,0x1234fff8,0xfffffff8};
    for(unsigned int n=0;n<4;n++) {
        probe(); save(0x10,seeds[n]);save(0x14,seeds[n]|3);
        assert(!mtk_dsi_poweron(&dsi));
        assert(load(0x10)==seeds[n] && load(0x14)==(seeds[n]&~0x100003U));
        assert(ulps_polls==1 && !pending_sleep && !(load(0)&4) && !(load(8)&0x8040));
        unsigned int before=ulps_polls;assert(!mtk_dsi_poweron(&dsi));assert(ulps_polls==before);
        mtk_dsi_poweroff(&dsi);assert(ulps_polls==before);
        off();assert(ulps_polls==before+1 && load(0x10)==seeds[n]);
        probe();save(0x10,seeds[n]);fail_ulps=true;
        assert(mtk_dsi_poweron(&dsi)==-ETIMEDOUT && !dsi.refcount && !dsi.lanes_ready);
        assert(!dsi.lock.held && irq_depth==1 && !engine_on && !digital_on && !phy_on);
        assert(load(8)==0 && !(load(0)&4) && !(load(0x14)&0x100000) && load(0x10)==seeds[n]);
        assert(!pending_sleep && ulps_polls==1 && resets==2);
        fail_ulps=false;assert(!mtk_dsi_poweron(&dsi));assert(dsi.lanes_ready);off();
        power();unsigned int resets_before=resets;fail_ulps=true;off();
        assert(!pending_sleep && resets==resets_before+1 && !dsi.lanes_ready);
    }
    /* Whole register fields must survive both normal and temporary mode changes. */
    for(unsigned int legacy=0;legacy<2;legacy++) for(unsigned int mode=0;mode<4;mode++) {
        power();dsi.lock.held=true;
        dsi.driver_data=legacy?&mt8183_dsi_driver_data:&mt6765_dsi_driver_data;
        dsi.mode_flags=mode?MIPI_DSI_MODE_VIDEO:0;
        if(mode==1) dsi.mode_flags|=MIPI_DSI_MODE_VIDEO_SYNC_PULSE;
        if(mode==3) dsi.mode_flags|=MIPI_DSI_MODE_VIDEO_BURST;
        save(0x14,0x5a1f0303);mtk_dsi_set_mode(&dsi);
        assert(load(0x14)==((legacy?0:0x5a1f0300)|mode));
        mtk_dsi_set_cmd_mode(&dsi);assert(load(0x14)==(legacy?0:0x5a1f0300));
        dsi.driver_data=&mt6765_dsi_driver_data;dsi.lock.held=false;save(0x14,0);off();
    }
    for(unsigned int mode=0;mode<4;mode++) {
        power();save(0x14,mode);save(0xc,0x80000008U);extra_vm=true;
        off();assert(last_steps[0]==2 && waits==1);
        power();save(0x14,mode);save(0xc,0x80000008U);fail_wait=1;
        off();assert(resets==1 && waits==1);
    }
    puts("PASS: native sleep/wake failures, retries, mode fields, reserved bit and legacy enable/mode checks");
}
int main(int argc,char **argv)
{
    if(argc==1) return command_suite();
    if(argc==2) { power_failures();return 0; }
    assert(argc==7);
    unsigned int op=strtoul(argv[1],NULL,0), seed=strtoul(argv[2],NULL,0);
    probe();engine_on=digital_on=phy_on=true;dsi.lock.held=true;
    dsi.data_rate=strtoul(argv[3],NULL,0);dsi.lanes=strtoul(argv[4],NULL,0);
    fail_ulps=atoi(argv[5]);
    save(0,seed&~1U);save(8,seed&~0x8040U);save(0x10,seed&~5U);
    save(0x14,seed);save(0x18,seed);save(0xa0,seed);
    save(0x104,op==1?seed&~2U:seed|2);save(0x108,op==1?seed&~2U:seed|2);
    u32 flag=op==1?0x8000:0x40;
    save(0xc,0x40001234|flag);recording=true;
    int result=0;
    if(op<2) result=mt6765_dsi_ulps(&dsi,op==1);
    else if(op==2) mtk_dsi_reset_engine(&dsi);
    else {
        unsigned int mode=atoi(argv[6]);dsi.mode_flags=mode?MIPI_DSI_MODE_VIDEO:0;
        if(mode==1) dsi.mode_flags|=MIPI_DSI_MODE_VIDEO_SYNC_PULSE;
        if(mode==3) dsi.mode_flags|=MIPI_DSI_MODE_VIDEO_BURST;
        mtk_dsi_set_mode(&dsi);
    }
    printf("{\"ret\":%d,\"writes\":[",result);
    for(unsigned int n=0;n<nevents;n++) printf("%s[%u,%u]",n?",":"",events[n][0],events[n][1]);
    printf("],\"status\":%u,\"pending\":%u}\n",load(0xc),pending_sleep);
    if(op<2) { assert(result==(fail_ulps?-ETIMEDOUT:0));assert(!pending_sleep); }
    return 0;
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'src/mainline/drivers/gpu/drm/mediatek/mtk_dsi.c')
    args=parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT):raise SystemExit('Source must stay under /rabbitr1')
    binary=command.build_harness(args.source,EXTRA)
    subprocess.run([str(binary),'power-failures'],check=True)
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    fixtures=[]
    for op in ('enter','exit','reset','mode'):
        for seed in (0,0x1234a5a0,0xfffffff8):
            for rate in ((125,130,260,500,1000,1500) if op=='exit' else (260,)):
                for lanes in (range(1,5) if op=='exit' else (2,)):
                    for wait in (('early','late','timeout') if op in ('enter','exit') else ('early',)):
                        for mode in (range(4) if op=='mode' else (0,)):
                            record=stock(raw,op,seed,rate,lanes,wait,mode)
                            got=json.loads(subprocess.check_output([str(binary),str(dict(exit=0,enter=1,reset=2,mode=3)[op]),
                                str(seed),str(rate*1000000),str(lanes),str(int(wait=='timeout')),str(mode)],text=True))
                            expected=record['writes']
                            assert [pair for pair in got['writes'] if pair[0]!=0xc]==expected,(record,got)
                            if op in ('enter','exit'):
                                flag=0x8000 if op=='enter' else 0x40
                                assert [pair for pair in got['writes'] if pair[0]==0xc]==[[0xc,(~flag)&0xffffffff]]*2
                                assert got['status']==0x40001234 and got['pending']==0
                            fixtures.append(record)
    matched = len(fixtures)
    # Exercise both complete stock power callbacks, including actual ULPS and resets.
    for op in ('on','off'):
        for seed in (0,2,0x1234a5a0):
            for wait in ('early','late','timeout'):
                record=stock(raw,op,seed,260,2,wait)
                assert int(record['registers']['0x10'])&2==seed&2
                assert all((value&2)==(seed&2) for off,value in record['writes'] if off==0x10)
                clock_calls=[call[0] for call in record['calls']]
                assert clock_calls==(['0x72a40c','0x715cb0','0x72a0a0','0x72a0a0'] if op=='on' else
                                     ['0x72a210','0x72a210','0x715cb0','0x72a40c'])
                fixtures.append(record)
    # Fractional rates and exact period boundaries must never undershoot 1 ms.
    for rate in (125000000,260004000,262143999,262144000,262144001,1499135999,1499136000,1500000000):
        got=json.loads(subprocess.check_output([str(binary),'0','0',str(rate),'2','0','0'],text=True))
        period=next(value for off,value in got['writes'] if off==0xa0)
        assert period*8192000>rate and (period-1)*8192000<=rate
    (ROOT/'out/dsi-power-audit.json').write_text(json.dumps(dict(fixtures=fixtures,hardware_tested=False,
        modeled='clocks, analog PHY calls, delays, scheduler and completion delivery; DSI routines execute'),indent=2)+'\n')
    print(f'PASS: {matched} native register traces match shipped sleep/wake/reset/mode routines; {len(fixtures)-matched} complete stock power calls preserve reserved bit 1')
    print('PASS: fractional wake periods exceed 1 ms; no physical lane transition has been tested')


if __name__=='__main__':main()
