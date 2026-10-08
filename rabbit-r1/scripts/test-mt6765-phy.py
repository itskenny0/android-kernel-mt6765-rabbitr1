#!/usr/bin/env python3
"""Compare the native MT6765 D-PHY sequence with selected stock instructions."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
import sys
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X30)

ROOT=Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
os.environ['TMPDIR']=str(ROOT/'.tmp')
DIRECTORY=ROOT/'src/mainline/drivers/phy/mediatek'
source=DIRECTORY/'phy-mtk-mipi-dsi-mt6765.c'
if len(sys.argv)==2:
    source=Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')


def stock(raw,params,calibration,initial,on):
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    bias,obj,mmio,stop=0x1000000,0x20000000,0x30000000,0x40000000
    uc.mem_map(bias,0x2000000); uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000); uc.mem_map(mmio,0x1000); uc.mem_map(stop,0x1000)
    uc.mem_write(bias+0x1a07f30,bytes(params))
    uc.mem_write(bias+0x1a07178,struct.pack('<Q',mmio))
    uc.mem_write(bias+0x1a0964c,struct.pack('<5I',*calibration))
    uc.mem_write(mmio,bytes(initial))
    for reg,value in [(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop),
            (UC_ARM64_REG_X0,13),(UC_ARM64_REG_X1,0),(UC_ARM64_REG_X2,on)]:
        uc.reg_write(reg,value)
    events=[]
    def code(uc,address,size,_):
        at=address-bias
        if at in (0x1f01c,0x747728,0xa5e2c,0x420ad0,0x7a2904,0xf1dee0):
            # Profiling/logging, normal display stage, constant microsecond delay.
            if at==0xf1dee0:
                ticks=uc.reg_read(UC_ARM64_REG_X0)
                assert ticks%4295==0
                events.append(('D',ticks//4295))
            if at!=0x1f01c: uc.reg_write(UC_ARM64_REG_X0,2 if at==0x7a2904 else 0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x714ae4<=at<0x715cb0,hex(at)
    def write(uc,access,address,size,value,_):
        if mmio<=address<mmio+0x1000:
            assert size==4
            events.append(('W',address-mmio,value))
        else:
            assert obj<=address and address+size<=obj+0x10000,hex(address)
    uc.hook_add(UC_HOOK_CODE,code); uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+0x715810,stop,count=20000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop
    assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert bytes(uc.mem_read(bias+0x1a07f30,len(params)))==bytes(params)
    return events,bytes(uc.mem_read(mmio,0x1000))


prelude=r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define FIELD_PREP(m,v) (((u32)(v)<<__builtin_ctz(m)) & (m))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define clamp_val(v,l,h) ((v)<(l)?(l):(v)>(h)?(h):(v))
#define div_u64(n,d) ((u64)(n)/(d))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define dev_dbg(...) ((void)0)
struct device { int unused; };
struct clk { int kind; };
struct clk_hw { struct clk *clk; struct clk_hw *parent; unsigned long rate; };
struct phy { void *data; };
struct clk_rate_request { unsigned long rate; };
struct clk_ops {
    int (*enable)(struct clk_hw *);
    void (*disable)(struct clk_hw *);
    int (*determine_rate)(struct clk_hw *,struct clk_rate_request *);
    int (*set_rate)(struct clk_hw *,unsigned long,unsigned long);
    unsigned long (*recalc_rate)(struct clk_hw *,unsigned long);
};
struct of_device_id { const char *compatible; const void *data; };
static unsigned char regs[0x1000];
static unsigned int writes,ref_count,pll_count,atomic_clock,clock_calls;
static int fail_ref,fail_pll;
static void *phy_get_drvdata(struct phy *p) { return p->data; }
static u32 readl(const void *p)
{ assert((const u8 *)p>=regs && (const u8 *)p+4<=regs+sizeof(regs)); assert(ref_count); u32 v; memcpy(&v,p,4); return v; }
static void writel(u32 v,void *p)
{ (void)readl(p); memcpy(p,&v,4); writes++; printf("W %zx %x\n",(u8 *)p-regs,v); }
static void udelay(unsigned int us) { assert(atomic_clock && ref_count); printf("D %x\n",us); }
static void usleep_range(unsigned int lo,unsigned int hi)
{ assert(!atomic_clock && ref_count && hi>=lo); printf("D %x\n",lo); }
static struct clk_hw *clk_hw_get_parent(struct clk_hw *hw) { return hw->parent; }
static unsigned long clk_hw_get_rate(struct clk_hw *hw) { return hw->rate; }
static unsigned long clk_get_rate(struct clk *clk);
static int clk_prepare_enable(struct clk *clk);
static void clk_disable_unprepare(struct clk *clk);
'''
clock_model=r'''
static struct clk ref={0},pll={1};
static struct clk_hw parent={ .clk=&ref,.rate=26000000 };
static struct mtk_mipi_tx tx={ .regs=regs,.ref_clk=&ref,.pll_hw={ .clk=&pll,.parent=&parent } };
static unsigned long clk_get_rate(struct clk *clk)
{ assert(!atomic_clock && clk==&ref); return parent.rate; }
static int clk_prepare_enable(struct clk *clk)
{
    assert(!atomic_clock); clock_calls++;
    if (clk==&ref) { if (fail_ref) return fail_ref; ref_count++; return 0; }
    assert(clk==&pll && !pll_count);
    if (fail_pll) return fail_pll;
    ref_count++; atomic_clock++;
    int ret=tx.driver_data->mipi_tx_clk_ops->enable(&tx.pll_hw);
    atomic_clock--;
    if (ret) ref_count--; else pll_count++;
    return ret;
}
static void clk_disable_unprepare(struct clk *clk)
{
    assert(!atomic_clock && ref_count);
    if (clk==&ref) { ref_count--; return; }
    assert(clk==&pll && pll_count==1); atomic_clock++;
    tx.driver_data->mipi_tx_clk_ops->disable(&tx.pll_hw);
    atomic_clock--; pll_count--; ref_count--;
}
static unsigned int legacy_step;
static int legacy_pll_on(struct clk_hw *hw) { (void)hw; assert(legacy_step++==0); return 0; }
static void legacy_signal_on(struct phy *phy) { (void)phy; assert(legacy_step++==1 && pll_count); }
static void legacy_signal_off(struct phy *phy) { (void)phy; assert(legacy_step++==2 && pll_count); }
static void legacy_pll_off(struct clk_hw *hw) { (void)hw; assert(legacy_step++==3); }
static const struct clk_ops legacy_ops={ .enable=legacy_pll_on,.disable=legacy_pll_off };
static const struct mtk_mipitx_data mt2701_mipitx_data={0},mt8173_mipitx_data={0};
static const struct mtk_mipitx_data mt8183_mipitx_data={ .mipi_tx_clk_ops=&legacy_ops,
    .mipi_tx_enable_signal=legacy_signal_on,.mipi_tx_disable_signal=legacy_signal_off };
'''
main=r'''
static const struct mtk_mipitx_data *match(const char *name)
{
    for (unsigned int i=0;mtk_mipi_tx_match[i].compatible;i++)
        if (!strcmp(mtk_mipi_tx_match[i].compatible,name)) return mtk_mipi_tx_match[i].data;
    assert(false); return NULL;
}
static void check_errors(struct phy *phy)
{
    const struct clk_ops *ops=tx.driver_data->mipi_tx_clk_ops;
    struct clk_rate_request req={0}; ops->determine_rate(&tx.pll_hw,&req); assert(req.rate==125000000);
    req.rate=ULONG_MAX; ops->determine_rate(&tx.pll_hw,&req); assert(req.rate==2500000000UL);
    const unsigned long bad[]={0,124999999,2500000001UL,ULONG_MAX};
    for (unsigned int i=0;i<ARRAY_SIZE(bad);i++) {
        tx.data_rate=260000000;
        assert(ops->set_rate(&tx.pll_hw,bad[i],26000000)==-EINVAL && tx.data_rate==260000000);
        if (bad[i]<=UINT32_MAX) {
            tx.data_rate=bad[i]; assert(mtk_mipi_tx_power_on(phy)==-EINVAL);
            assert(ops->enable(&tx.pll_hw)==-EINVAL);
            assert(!ref_count && !pll_count && !writes && !clock_calls && !tx.rt_code_saved);
        }
    }
    tx.data_rate=260000000; parent.rate=24000000;
    assert(ops->set_rate(&tx.pll_hw,260000000,24000000)==-EINVAL);
    assert(mtk_mipi_tx_power_on(phy)==-EINVAL && !writes && !clock_calls);
    assert(ops->enable(&tx.pll_hw)==-EINVAL && !writes);
    tx.pll_hw.parent=NULL; assert(ops->enable(&tx.pll_hw)==-EINVAL && !writes);
    tx.pll_hw.parent=&parent;
    parent.rate=26000000; fail_ref=-EIO;
    assert(mtk_mipi_tx_power_on(phy)==-EIO && !ref_count && !pll_count && !writes && !tx.rt_code_saved);
    fail_ref=0; fail_pll=-EIO;
    assert(mtk_mipi_tx_power_on(phy)==-EIO && !ref_count && !pll_count && tx.rt_code_saved);
    assert(*(u32 *)(regs+0xc)==0x3fff0100 && !(*(u32 *)(regs+0x30)&16));
    fail_pll=0;
    assert(mtk_mipi_tx_power_on(phy)==0 && ref_count==2 && pll_count==1);
    assert(mtk_mipi_tx_power_off(phy)==0 && !ref_count && !pll_count);
    tx.driver_data=match("mediatek,mt8183-mipi-tx");
    fail_pll=-EIO;
    assert(mtk_mipi_tx_power_on(phy)==-EIO && !ref_count && !pll_count && !legacy_step);
    fail_pll=0;
    assert(mtk_mipi_tx_power_on(phy)==0 && ref_count==1 && pll_count==1);
    assert(mtk_mipi_tx_power_off(phy)==0 && !ref_count && !pll_count && legacy_step==4);
    puts("PASS errors and legacy ordering");
}
int main(int argc,char **argv)
{
    assert(argc==5);
    unsigned long rate=strtoul(argv[1],NULL,0);
    u32 seed=strtoul(argv[2],NULL,0),codes[5];
    unsigned int pattern=atoi(argv[3]),mode=atoi(argv[4]);
    struct phy phy={ .data=&tx };
    tx.driver_data=match("mediatek,mt6765-mipi-tx");
    assert(tx.driver_data->preserve_fw_calibration);
    for (unsigned int i=0;i<sizeof(regs);i+=4) memcpy(regs+i,&seed,4);
    for (unsigned int i=0;i<5;i++) {
        codes[i]=pattern?(0x155U*(i+1))&0x3ff:0;
        for (unsigned int j=0;j<10;j++) {
            u32 value=(codes[i]>>j)&1; memcpy(regs+0x100*(i+1)+4*j,&value,4);
        }
    }
    if (mode==2) { check_errors(&phy); return 0; }
    assert(tx.driver_data->mipi_tx_clk_ops->set_rate(&tx.pll_hw,rate,26000000)==0);
    assert(tx.driver_data->mipi_tx_clk_ops->recalc_rate(&tx.pll_hw,26000000)==rate);
    unsigned int cycles=mode==1?2:1;
    for (unsigned int cycle=0;cycle<cycles;cycle++) {
        assert(mtk_mipi_tx_power_on(&phy)==0 && pll_count==1 && ref_count==2);
        assert(tx.rt_code_saved && !memcmp(codes,tx.rt_code,sizeof(codes)));
        assert(mtk_mipi_tx_power_off(&phy)==0 && !pll_count && !ref_count);
        if (cycle+1<cycles) {
            // Model calibration loss between power cycles; saved values must survive.
            for (unsigned int i=0;i<5;i++) memset(regs+0x100*(i+1),0,40);
        }
    }
    u32 untouched[]={0x10,0x40,0x44,0x48,0x4c};
    for (unsigned int i=0;i<ARRAY_SIZE(untouched);i++) assert(*(u32 *)(regs+untouched[i])==seed);
    puts("PASS sequence");
}
'''


def harness():
    def block(name,s):
        m=re.search(r'^.*\b'+re.escape(name)+r'(?:\(|\s*\{|\s*(?:\[\])?\s*=)',s,re.M)
        assert m,name
        start=m.start(); end=s.index('\n}',start)+2
        if s[end:end+1]==';': end+=1
        return s[start:end]+'\n'
    header=(DIRECTORY/'phy-mtk-mipi-dsi.h').read_text()
    common=(DIRECTORY/'phy-mtk-mipi-dsi.c').read_text()
    io=(DIRECTORY/'phy-mtk-io.h').read_text()
    io='\n'.join(line for line in io.splitlines() if not line.startswith('#include'))
    io=io.replace('BUILD_BUG_ON_MSG(!__builtin_constant_p(mask), "mask is not constant");','')
    native='\n'.join(line for line in source.read_text().splitlines() if not line.startswith('#include'))
    code=prelude+block('mtk_mipitx_data',header)+block('mtk_mipi_tx',header)
    code+=block('mtk_mipi_tx_from_clk_hw',common).replace('inline struct','static inline struct')
    code+=block('mtk_mipi_tx_pll_set_rate',common)+block('mtk_mipi_tx_pll_recalc_rate',common)
    code+=io+'\n'+native+'\n'+clock_model
    code+=block('mtk_mipi_tx_power_on',common)+block('mtk_mipi_tx_power_off',common)
    code+=block('mtk_mipi_tx_match',common)+main
    path=ROOT/'out/mt6765-phy-host.c'; path.write_text(code)
    binary=ROOT/'out/mt6765-phy-host'
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-fsanitize=address,undefined','-fno-pie','-no-pie',str(path),'-o',str(binary)],check=True)
    return binary


def main_test():
    spec=importlib.util.spec_from_file_location('panel',ROOT/'scripts/test-r1-panel.py')
    panel=importlib.util.module_from_spec(spec); spec.loader.exec_module(panel)
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==panel.IMAGE_SHA256
    _,full=panel.stock_trace(raw,0x6eed08); original=full[472:]
    assert struct.unpack_from('<I',original,220)[0]==1, 'r1 SSC must be disabled'
    binary=harness()
    def host(rate,seed=0,pattern=1,mode=0):
        lines=subprocess.check_output([str(binary),str(rate),str(seed),str(pattern),str(mode)],text=True).splitlines()
        assert lines[-1]==('PASS errors and legacy ordering' if mode==2 else 'PASS sequence')
        return [tuple([line.split()[0],*[int(n,16) for n in line.split()[1:]]]) for line in lines[:-1]]
    def expected(rate,seed,pattern,mode):
        params=bytearray(original)
        struct.pack_into('<I',params,196,0); struct.pack_into('<I',params,204,rate)
        codes=[(0x155*(i+1))&0x3ff if pattern else 0 for i in range(5)]
        state=bytearray(struct.pack('<I',seed)*1024)
        for i,value in enumerate(codes):
            for bit in range(10): struct.pack_into('<I',state,0x100*(i+1)+bit*4,(value>>bit)&1)
        trace=[]
        for cycle in range(2 if mode==1 else 1):
            if struct.unpack_from('<I',state,0x30)[0]&16:
                events,state=stock(raw,params,codes,state,0); trace+=events
            for on in (1,0):
                events,state=stock(raw,params,codes,state,on); trace+=events
            if cycle==0 and mode==1:
                state=bytearray(state)
                for i in range(5): state[0x100*(i+1):0x100*(i+1)+40]=bytes(40)
        return trace
    rates=[125,126,200,249,250,251,260,499,500,501,750,999,1000,1001,1500,1999,2000,2001,2500]
    cases=0
    for rate in rates:
        for seed in (0,0xa5a50000,0xffffffff):
            actual=host(rate*1000000,seed)
            reference=expected(rate,seed,1,0)
            assert actual==reference,(rate,hex(seed),[(a,b) for a,b in zip(actual,reference) if a!=b][:3],len(actual),len(reference))
            cases+=1
    for pattern in (0,1):
        for seed in (0,0xffffffff):
            assert host(260000000,seed,pattern,1)==expected(260,seed,pattern,1)
    fractional=host(260004000)
    reference=expected(260,0,1,0)
    # Stock accepts integer Mbit/s only. Check fractional PCW separately.
    pcw=(260004000*8<<24)//26000000
    reference=[('W',0x2c,pcw) if e[:2]==('W',0x2c) else e for e in reference]
    assert fractional==reference
    host(260000000,pattern=1,mode=2)
    report={'stock_image_sha256':panel.IMAGE_SHA256,'rates_mbps':rates,'exact_sequence_cases':cases,
        'repeated_cycles':4,'r1_fractional_pcw':hex(pcw),
        'scope':'selected stock instructions vs production PHY and common power callbacks; modeled clocks/MMIO/delays; no hardware'}
    (ROOT/'out/mt6765-phy-audit.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'PASS: {cases} exact stock PHY setup/shutdown sequences; four calibration restore cycles; fractional PCW')
    print('PASS: rate/reference-clock bounds, atomic clock context, error cleanup and legacy power ordering')
    print('No analog circuit, PLL lock, signal integrity, physical clock or display was tested.')


if __name__=='__main__': main_test()
