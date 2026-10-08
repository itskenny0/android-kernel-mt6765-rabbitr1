#!/usr/bin/env python3
"""Audit MT6765 IOMMU controller setup and its software power lifecycle."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X19, UC_ARM64_REG_X20,
    UC_ARM64_REG_X21, UC_ARM64_REG_X22, UC_ARM64_REG_X23, UC_ARM64_REG_X24,
    UC_ARM64_REG_X25, UC_ARM64_REG_X26, UC_ARM64_REG_X27, UC_ARM64_REG_X28,
    UC_ARM64_REG_X29, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
spec = importlib.util.spec_from_file_location('smi', Path(__file__).with_name('test-mt6765-smi.py'))
smi = importlib.util.module_from_spec(spec); spec.loader.exec_module(smi)


def stock_init(raw, seed, protect, ttbr):
    bias, obj, mmio, stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, 0x2000000); uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000); uc.mem_map(mmio, 0x1000); uc.mem_map(stop, 0x1000)
    uc.mem_write(mmio, struct.pack('<I', seed)*0x400)
    uc.mem_write(bias+0x1aa4740, struct.pack('<Q', mmio))
    uc.mem_write(obj+8, struct.pack('<Q', ttbr))
    saved = [UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21, UC_ARM64_REG_X22,
             UC_ARM64_REG_X23, UC_ARM64_REG_X24, UC_ARM64_REG_X25, UC_ARM64_REG_X26,
             UC_ARM64_REG_X27, UC_ARM64_REG_X28, UC_ARM64_REG_X29]
    for i, reg in enumerate(saved): uc.reg_write(reg, 0x12340000+i)
    for reg, value in [(UC_ARM64_REG_SP, obj+0x8000), (UC_ARM64_REG_X30, stop),
                       (UC_ARM64_REG_X0, obj), (UC_ARM64_REG_X1, protect), (UC_ARM64_REG_X2, 0)]:
        uc.reg_write(reg, value)
    writes, larbs = [], []
    mapped = 0
    locked = False
    lock_count = 0

    def code(uc, address, size, _):
        nonlocal mapped, locked, lock_count
        at = address-bias
        if at in (0x1f01c, 0xa5e2c, 0xb156dc, 0xb1c474, 0xf38530, 0xf386b0):
            value = 0
            if at == 0xb156dc:
                assert not uc.reg_read(UC_ARM64_REG_X0) and not uc.reg_read(UC_ARM64_REG_X1)
                p = uc.reg_read(UC_ARM64_REG_X2)-0xffffff8008080000
                assert 0 <= p < len(raw)
                larbs.append(raw[p:raw.index(b'\0', p)].decode())
                assert len(larbs) == mapped+1
                value = obj+0x100+mapped*16
            elif at == 0xb1c474:
                assert uc.reg_read(UC_ARM64_REG_X0) == obj+0x100+mapped*16
                assert uc.reg_read(UC_ARM64_REG_X1) == 0
                value = 0x31000000+mapped*0x1000; mapped += 1
            elif at in (0xf38530, 0xf386b0):
                assert uc.reg_read(UC_ARM64_REG_X0) == bias+0x1aa4748
                assert locked == (at == 0xf386b0)
                locked = not locked
                if locked: lock_count += 1
            if at != 0x1f01c: uc.reg_write(UC_ARM64_REG_X0, value)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x8a370c <= at < 0x8a3984 or 0x89ed3c <= at < 0x89ee28, hex(at)

    def write(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            assert size == 4
            if address-mmio in (0x38, 0x20): assert locked
            writes.append((address-mmio, value))
        else:
            assert (obj+0x7000 <= address and address+size <= obj+0x8000 or
                    bias+0x1aa4750 <= address < bias+0x1aa4770 and size == 8), hex(address)

    uc.hook_add(UC_HOOK_CODE, code); uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.emu_start(bias+0x8a370c, stop, count=4000)
    assert uc.reg_read(UC_ARM64_REG_PC) == stop and uc.reg_read(UC_ARM64_REG_SP) == obj+0x8000
    assert [uc.reg_read(reg) for reg in saved] == [0x12340000+i for i in range(len(saved))]
    assert uc.reg_read(UC_ARM64_REG_X0) == 0 and mapped == 4 and not locked and lock_count == 1
    expected = [(0,ttbr),(4,ttbr),(0x110,seed|0x20),(0x80,3),(0x88,0),
                (0x120,0x6f),(0x124,0xffffffff),(0x114,(protect&0xffffff80)|((protect>>32)&3)),
                (0x50,0),(0x38,3),(0x20,2),(0x84,0),(0x48,0),
                (0x54,seed&~0xc00),(0x44,seed&~1)]
    assert writes == expected, writes
    return dict(seed=seed, protect=protect, ttbr=ttbr, larbs=larbs, writes=writes)


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dt-bindings/memory/mtk-memory-port.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
typedef uint64_t phys_addr_t;
typedef int spinlock_t;
typedef int irqreturn_t;
#define __iomem
#define __maybe_unused
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define lower_32_bits(n) ((u32)(n))
#define upper_32_bits(n) ((u32)((uint64_t)(n)>>32))
#define SZ_4G (1ULL<<32)
#define SZ_8M (1ULL<<23)
#define MTK_IOMMU_IOVA_SZ_4G (SZ_4G-SZ_8M)
#define dev_err(...) ((void)0)
#define DMA_BIT_MASK(n) ((1ULL<<(n))-1)
struct device { void *data; };
struct mutex { unsigned int held; };
struct list_head { int unused; };
struct iommu_domain { int unused; };
struct mtk_iommu_domain {
    struct iommu_domain domain;
    struct mutex mutex;
    struct mtk_iommu_bank_data *bank;
    struct { struct { u32 ttbr; } arm_v7s_cfg; } cfg;
};
'''
MODEL = r'''
struct mtk_iommu_data {
    struct device *dev;
    void *bclk;
    phys_addr_t protect_base;
    struct mtk_iommu_suspend_reg reg;
    bool enable_4GB;
    const struct mtk_iommu_plat_data *plat_data;
    struct mtk_iommu_bank_data *bank;
    struct list_head *hw_list;
    struct mutex mutex;
};
static unsigned char regs[0x1000];
static struct mtk_iommu_data *current;
static u32 expected_ttbr;
static unsigned int refs,reads,writes,root_writes,irqs,flushes,configs,finalises;
static unsigned int offsets[128];
static u32 values[128];
static int clock_error,irq_error,domain_error,region_error;
static bool trace,strict_order;
static u32 load(unsigned int off) { u32 v; memcpy(&v,regs+off,4); return v; }
static void save(unsigned int off,u32 v) { memcpy(regs+off,&v,4); }
static u32 readl_relaxed(const void *p) {
    assert(refs==1 && (const u8 *)p>=regs && (const u8 *)p+4<=regs+sizeof(regs));
    u32 v; memcpy(&v,p,4); reads++; return v;
}
static void writel_relaxed(u32 v,void *p) {
    assert(refs==1 && (u8 *)p>=regs && (u8 *)p+4<=regs+sizeof(regs));
    unsigned int off=(u8 *)p-regs; assert(writes<ARRAY_SIZE(values));
    if (off==0 && v==expected_ttbr) root_writes++;
    if (off==0x88 && !v && strict_order) assert(root_writes && load(0)==expected_ttbr);
    offsets[writes]=off; values[writes++]=v; save(off,v);
    if (trace) printf("WRITE %u %u\n",off,v);
}
#define writel writel_relaxed
static int clk_prepare_enable(void *clk) {
    assert(current && clk==current->bclk && !refs);
    if (clock_error) return clock_error;
    refs=1; return 0;
}
static void clk_disable_unprepare(void *clk) {
    assert(clk==current->bclk && refs==1); refs=0;
}
static void *dev_get_drvdata(struct device *dev) { return dev->data; }
static const char *dev_name(struct device *dev) { assert(dev==current->dev); return "iommu-test"; }
static irqreturn_t mtk_iommu_isr(int irq,void *data) { (void)irq;(void)data; assert(!"IRQ delivery not modeled"); return 0; }
static int devm_request_irq(struct device *dev,int irq,irqreturn_t (*fn)(int,void *),
    unsigned long flags,const char *name,void *data) {
    assert(dev==current->dev && irq==73 && fn==mtk_iommu_isr && !flags && name && data==current->bank);
    assert(refs==1); irqs++; return irq_error;
}
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held=1; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held=0; }
#define spin_lock_irqsave(p,f) do { assert(!*(p)); *(p)=1; (f)=0; } while (0)
#define spin_unlock_irqrestore(p,f) do { assert(*(p)==1 && !(f)); *(p)=0; } while (0)
static void wmb(void) {
    assert(refs==1 && load(current->plat_data->inv_sel_reg)==3 && load(0x20)==2);
    if (strict_order && current->bank[0].m4u_dom)
        assert(root_writes && load(0)==expected_ttbr && !load(0x88));
    flushes++;
}
static struct mtk_iommu_data *dev_iommu_priv_get(struct device *dev) { assert(dev); return current; }
static struct mtk_iommu_domain *to_mtk_domain(struct iommu_domain *dom)
{ return (void *)((u8 *)dom - offsetof(struct mtk_iommu_domain,domain)); }
static int mtk_iommu_get_iova_region_id(struct device *dev,const struct mtk_iommu_plat_data *data)
{ (void)dev; assert(data==current->plat_data); return region_error; }
static unsigned int mtk_iommu_get_bank_id(struct device *dev,const struct mtk_iommu_plat_data *data)
{ (void)dev; assert(data==current->plat_data); return 0; }
static struct mtk_iommu_data *mtk_iommu_get_frst_data(struct list_head *list)
{ assert(list==current->hw_list); return current; }
static int mtk_iommu_domain_finalise(struct mtk_iommu_domain *dom,struct mtk_iommu_data *data,unsigned int region)
{ assert(dom->mutex.held && data==current && data->mutex.held && !region); finalises++; return domain_error; }
static int mtk_iommu_config(struct mtk_iommu_data *data,struct device *dev,bool enable,unsigned int region)
{ assert(data==current && dev && enable && !region && !refs); configs++; return 0; }
static int dma_set_mask_and_coherent(struct device *dev,u64 mask)
{ (void)dev;(void)mask; assert(!"unexpected high IOVA region"); return 0; }
static int mtk_iommu_runtime_resume(struct device *dev);
static int mtk_iommu_runtime_suspend(struct device *dev);
static int pm_runtime_resume_and_get(struct device *dev) { return mtk_iommu_runtime_resume(dev); }
/* Model immediate suspend when the final reference is put. */
static void pm_runtime_put(struct device *dev) { assert(!mtk_iommu_runtime_suspend(dev)); }
'''
MAIN = r'''
static void fill(u32 value) {
    assert(!refs);
    for (unsigned int off=0;off<sizeof(regs);off+=4) save(off,value);
    reads=writes=root_writes=0;
}
static void expected_setup(u32 seed,bool native) {
    bool mt6779=current->plat_data->m4u_plat==M4U_MT6779;
    assert(load(0x54)==(seed&~(native?0xc00:mt6779?0x200020:0)));
    assert(load(0x44)==(native?seed&~1:seed));
    assert(load(0x80)==(native?3:seed));
    assert(load(0x84)==(native?0:seed));
    assert(load(0x88)==(native?0:seed));
    assert(load(0x110)==(seed|0x20) && load(0x50)==0);
    assert(load(0x48)==(mt6779?seed&~0xa000a:0));
    assert(load(0x120)==0x6f && load(0x124)==0x3fff);
    assert(load(0x114)==(lower_32_bits(current->protect_base)|upper_32_bits(current->protect_base)));
    assert(load(4)==seed); /* Secure page-table ownership is unchanged. */
    if (native) assert(load(0x38)==3 && load(0x20)==2);
    for (unsigned int off=4;off<sizeof(regs);off+=4) {
        if (off==0x48 || off==0x50 || off==0x54 || off==0x110 || off==0x114 ||
            off==0x120 || off==0x124) continue;
        if (native && (off==0x20 || off==0x38 || off==0x44 || off==0x80 ||
                       off==0x84 || off==0x88)) continue;
        assert(load(off)==seed);
    }
}
static void lifecycle(u32 seed,u64 protect,const struct mtk_iommu_plat_data *pdata)
{
    bool native=pdata->m4u_plat==M4U_MT6765;
    struct device dev={0},client={0}; struct list_head list={0};
    struct mtk_iommu_bank_data bank={.base=regs,.irq=73,.parent_dev=&dev};
    struct mtk_iommu_data data={.dev=&dev,.bank=&bank,.protect_base=protect,.hw_list=&list,
        .plat_data=pdata};
    struct mtk_iommu_domain dom={.cfg.arm_v7s_cfg.ttbr=expected_ttbr};
    dev.data=&data; bank.parent_data=&data; current=&data;
    strict_order=native; fill(seed); irqs=flushes=configs=finalises=0;
    clock_error=irq_error=domain_error=region_error=0;
    region_error=-EINVAL;
    assert(mtk_iommu_attach_device(&dom.domain,&client,NULL)==-EINVAL);
    assert(!refs && !reads && !writes && !finalises);
    region_error=0; domain_error=-ENOMEM;
    assert(mtk_iommu_attach_device(&dom.domain,&client,NULL)==-ENOMEM);
    assert(!refs && !reads && !writes && !dom.bank && !data.mutex.held && !dom.mutex.held);
    domain_error=0; clock_error=-EIO;
    assert(mtk_iommu_attach_device(&dom.domain,&client,NULL)==-EIO);
    assert(!refs && !reads && !writes && !bank.m4u_dom);
    clock_error=0;
    /* Observe startup through its real attach call site, not a manually seeded root. */
    assert(!mtk_iommu_attach_device(&dom.domain,&client,NULL));
    assert(!refs && bank.m4u_dom==&dom && configs==1 && irqs==1 && !data.mutex.held);
    assert(flushes==(native?1:0));
    expected_setup(seed,native); assert(load(0)==expected_ttbr);
    if (native) assert(root_writes==2); else assert(root_writes==1);
    unsigned int before=writes;
    assert(!mtk_iommu_attach_device(&dom.domain,&client,NULL));
    assert(writes==before && irqs==1 && configs==2); /* Existing bank isn't reinitialized. */
    bool old_trace=trace; trace=false;
    for (unsigned int cycle=0;cycle<3;cycle++) {
        /* Zero WR_LEN_CTRL is a valid snapshot, not an uninitialized marker. */
        u32 wr=cycle==1?0:cycle==2?0xc00:seed;
        assert(!mtk_iommu_runtime_resume(&dev)); save(0x54,wr);
        assert(!mtk_iommu_runtime_suspend(&dev));
        u32 saved_ctrl=data.reg.ctrl_reg, saved_ivrp=data.reg.ivrp_paddr[0];
        fill(0x5a5a5a5b); clock_error=-EIO;
        assert(mtk_iommu_runtime_resume(&dev)==-EIO && !refs && !reads && !writes);
        clock_error=0; unsigned int old_flushes=flushes;
        assert(!mtk_iommu_runtime_resume(&dev) && refs==1);
        assert(load(0x54)==wr && load(0)==expected_ttbr && root_writes==1);
        assert(load(0x110)==saved_ctrl && load(0x114)==saved_ivrp);
        assert(load(0x120)==0x6f && load(0x124)==0x3fff && flushes==old_flushes+1);
        assert(load(4)==0x5a5a5a5b);
        assert(load(0x44)==(native?0x5a5a5a5a:0x5a5a5a5b));
        assert(load(0x80)==(native?3:0x5a5a5a5b));
        assert(load(0x84)==(native?0:0x5a5a5a5b));
        assert(load(0x88)==(native?0:0x5a5a5a5b));
        assert(!mtk_iommu_runtime_suspend(&dev));
    }
    trace=old_trace;
    /* Initial clock-only resume must not consume a never-saved register image. */
    memset(&data.reg,0,sizeof(data.reg)); bank.m4u_dom=NULL; fill(seed);
    assert(!mtk_iommu_runtime_resume(&dev) && refs==1 && !reads && !writes);
    clk_disable_unprepare(data.bclk);
    /* An IRQ allocation failure must not leave a usable page-table root. */
    memset(&data.reg,0,sizeof(data.reg)); dom.bank=NULL; fill(seed); irq_error=-EBUSY;
    old_trace=trace; trace=false;
    assert(mtk_iommu_attach_device(&dom.domain,&client,NULL)==-ENODEV);
    assert(!refs && !bank.m4u_dom && !load(0) && !data.mutex.held && !dom.mutex.held);
    irq_error=0;
    assert(!mtk_iommu_attach_device(&dom.domain,&client,NULL));
    assert(!refs && bank.m4u_dom==&dom && load(0)==expected_ttbr);
    trace=old_trace;
}
int main(int argc,char **argv)
{
    assert(argc==1 || argc==4);
    expected_ttbr=0x48004000;
    if (argc==4) {
        u32 seed=strtoul(argv[1],NULL,0); u64 protect=strtoull(argv[2],NULL,0);
        expected_ttbr=strtoul(argv[3],NULL,0); trace=true;
        lifecycle(seed,protect,&mt6765_data); return 0;
    }
    const u32 seeds[]={0xffffffff,0,0xa5a55a5a,0xc00,0x20,0x200000,0x200c20,0x3ff};
    for (unsigned int n=0;n<ARRAY_SIZE(seeds);n++) {
        lifecycle(seeds[n],0x50001000,&mt6765_data);
        lifecycle(seeds[n],0x50001000,&mt8183_data);
        lifecycle(seeds[n],0x50001000,&mt6779_data);
    }
    puts("PASS: IOMMU attach/setup, page-table-before-walk order, first resume, zero-valued snapshots, repeated suspend/resume and error cleanup");
    puts("MT8183/MT6779 register behavior checked; IRQ transport, PM scheduling, clocks and page-table allocation are modeled.");
}
'''


def build_harness(source):
    s = source.read_text()
    code = PRELUDE+s[s.index('#define REG_MMU_PT_BASE_ADDR'):s.index('enum mtk_iommu_plat')]
    for name in ('mtk_iommu_plat', 'mtk_iommu_iova_region', 'mtk_iommu_suspend_reg',
                 'mtk_iommu_plat_data', 'mtk_iommu_bank_data', 'single_domain',
                 'mt6765_data', 'mt8183_data', 'mt6779_data'):
        code += smi.block(s, name)
    code += MODEL
    if 'static void mt6765_iommu_config' in s: code += smi.block(s, 'mt6765_iommu_config')
    for name in ('mtk_iommu_tlb_flush_all', 'mtk_iommu_hw_init', 'mtk_iommu_runtime_suspend',
                 'mtk_iommu_runtime_resume', 'mtk_iommu_attach_device'):
        # hw_init has a forward declaration; match its function body instead.
        body = s[s.index('static int mtk_iommu_hw_init(', s.index('static int mtk_iommu_hw_init(')+1):] if name == 'mtk_iommu_hw_init' else s
        code += smi.block(body, name)
    path = ROOT/'out/mt6765-iommu-host.c'; path.write_text(code+MAIN)
    binary = ROOT/'out/mt6765-iommu-host'
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
        '-Wno-sign-compare', '-fsanitize=address,undefined', '-fno-pie', '-no-pie', '-O1', '-g',
        '-I'+str(SRC/'include'), str(path), '-o', str(binary)], check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'drivers/iommu/mtk_iommu.c')
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')
    binary = build_harness(args.source)
    subprocess.run([str(binary)], check=True)
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    fixtures = []
    for seed in (0, 0xffffffff, 0xa5a55a5a, 0xc00, 0x20, 0x200000, 0x200c20, 0x3ff):
        for protect in (0x50001000, 0x150001000, 0x350001000):
            fixture = stock_init(raw, seed, protect, 0x48004000)
            text = subprocess.check_output([str(binary),str(seed),str(protect),'0x48004000'], text=True)
            native = [tuple(map(int, line.split()[1:])) for line in text.splitlines()]
            actual = dict(native); stock = dict(fixture['writes'])
            # Native IRQ policy omits MAU monitoring; secure root is not Linux-owned.
            for off in (0,0x20,0x38,0x44,0x48,0x50,0x54,0x80,0x84,0x88,0x110,0x114,0x120):
                assert actual[off] == stock[off], (hex(off), actual, stock)
            assert actual[0x124] == stock[0x124] & 0x3fff and 4 not in actual
            fixture['native_startup_writes'] = native; fixtures.append(fixture)
    (ROOT/'out/mt6765-iommu-audit.json').write_text(json.dumps(dict(
        stock_function='m4u_reg_init', raw_offset='0x8a370c', fixtures=fixtures,
        hardware_tested=False), indent=2)+'\n')
    print(f'PASS: {len(fixtures)} stock controller initializations with real full-TLB-invalidate instructions')
    print('No DMA, page-table walks, physical PM/IRQ behavior or complete firmware handoff is validated.')


if __name__ == '__main__': main()
