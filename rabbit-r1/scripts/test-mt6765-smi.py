#!/usr/bin/env python3
"""Compare native SMI port setup with the shipped M4U code and compiled DT."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21, UC_ARM64_REG_X22,
    UC_ARM64_REG_X23, UC_ARM64_REG_X24, UC_ARM64_REG_X25, UC_ARM64_REG_X26,
    UC_ARM64_REG_X27, UC_ARM64_REG_X28, UC_ARM64_REG_X29, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')


def stock_ports(raw):
    count = struct.unpack_from('<I', raw, 0x179e7a4)[0]
    assert count == 52
    ports = []
    for i in range(count):
        name, fields = struct.unpack_from('<QI', raw, 0x179ddb0+48*i)
        name -= 0xffffff8008080000
        assert 0 <= name < len(raw)
        ports.append(dict(index=i, name=raw[name:raw.index(b'\0', name)].decode(),
                          larb=(fields>>4)&15, port=(fields>>8)&255, m4u=fields&3))
    assert [sum(p['larb'] == i for p in ports) for i in range(4)] == [8, 11, 12, 21]
    assert all(p['m4u'] == 0 and p['port'] < 32 for p in ports)
    assert [p['name'] for p in ports[:3]] == ['DISP_OVL0', 'DISP_2L_OVL0_LARB0', 'DISP_RDMA0']
    return ports


def stock_config(raw, ports, seed):
    bias, obj, mmio, stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, 0x2000000); uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000); uc.mem_map(mmio, 0x4000); uc.mem_map(stop, 0x1000)
    uc.mem_write(bias+0x1aa4750, struct.pack('<4Q', *(mmio+0x1000*i for i in range(4))))
    saved = [UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21, UC_ARM64_REG_X22,
             UC_ARM64_REG_X23, UC_ARM64_REG_X24, UC_ARM64_REG_X25, UC_ARM64_REG_X26,
             UC_ARM64_REG_X27, UC_ARM64_REG_X28, UC_ARM64_REG_X29]
    fixture = None
    clocks, locked, ticks = 0, False, 0
    events, prefetch, trace = [], [], []

    def code(uc, address, size, _):
        nonlocal clocks, locked, ticks
        at = address-bias
        if at in (0x1f01c, 0x69f6bc, 0xd9a34, 0x8a0750, 0x8a07b0,
                  0x8a0c64, 0xf38530, 0xf386b0, 0xa5e2c):
            arg = uc.reg_read(UC_ARM64_REG_X0)
            if at == 0x8a0750:
                assert arg == fixture['larb'] and not clocks and not locked
                clocks = 1
            elif at == 0x8a07b0:
                assert arg == fixture['larb'] and clocks == 1 and not locked
                clocks = 0
            elif at == 0xf38530:
                assert arg == bias+0x1aa4748 and clocks == 1 and not locked
                locked = True
            elif at == 0xf386b0:
                assert arg == bias+0x1aa4748 and clocks == 1 and locked
                locked = False
            elif at == 0x8a0c64:
                assert arg == fixture['index'] and clocks == 1
                prefetch.append(arg)
            if at == 0xd9a34: ticks += 1
            if at != 0x1f01c: uc.reg_write(UC_ARM64_REG_X0, ticks if at == 0xd9a34 else 0)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x8a13cc <= at < 0x8a1914, hex(at)

    def access(uc, kind, address, size, value, _):
        if mmio <= address < mmio+0x4000:
            assert size == 4 and clocks == 1 and locked
            assert address-mmio == fixture['larb']*0x1000+0x380+4*fixture['port']

    def write(uc, kind, address, size, value, _):
        if mmio <= address < mmio+0x4000:
            access(uc, kind, address, size, value, _)
            events.append((address-mmio, value))
        else:
            assert obj+0x7000 <= address and address+size <= obj+0x8000, hex(address)

    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_READ, access)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    for fixture in ports:
        # Check both transitions, including dirty upper bits that must survive.
        for virt in (1, 0):
            initial = seed if virt else seed | 1
            uc.mem_write(mmio, struct.pack('<I', initial)*0x1000)
            config = struct.pack('<6I', fixture['index'], virt, 0, 0, 1, 0)
            uc.mem_write(obj, config)
            for i, reg in enumerate(saved): uc.reg_write(reg, 0x12340000+i)
            uc.reg_write(UC_ARM64_REG_SP, obj+0x8000)
            uc.reg_write(UC_ARM64_REG_X30, stop)
            uc.reg_write(UC_ARM64_REG_X0, obj)
            events.clear(); prefetch.clear()
            uc.emu_start(bias+0x8a13cc, stop, count=3000)
            assert uc.reg_read(UC_ARM64_REG_PC) == stop and uc.reg_read(UC_ARM64_REG_SP) == obj+0x8000
            assert [uc.reg_read(reg) for reg in saved] == [0x12340000+i for i in range(len(saved))]
            assert uc.reg_read(UC_ARM64_REG_X0) == 0 and not clocks and not locked
            assert bytes(uc.mem_read(obj, len(config))) == config
            off = fixture['larb']*0x1000+0x380+4*fixture['port']
            assert events == [(off, (initial & ~1) | virt)], events
            assert prefetch == [fixture['index']]*(1 if virt else 2)
            expected = bytearray(struct.pack('<I', initial)*0x1000)
            struct.pack_into('<I', expected, off, (initial & ~1) | virt)
            assert bytes(uc.mem_read(mmio, 0x4000)) == expected
            trace.append(dict(**fixture, seed=initial, virtual=virt, writes=list(events)))
    return trace


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dt-bindings/memory/mt2701-larb-port.h>
#include <dt-bindings/memory/mt6765-larb-port.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
#define __iomem
#define __maybe_unused
#define BIT(n) (1UL<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define upper_32_bits(n) ((u32)((uint64_t)(n)>>32))
#define SZ_4G (1ULL<<32)
#define SZ_8M (1ULL<<23)
#define MTK_IOMMU_IOVA_SZ_4G (SZ_4G-SZ_8M)
#define dev_err(...) ((void)0)
#define dev_dbg(...) ((void)0)
#define MTK_SIP_KERNEL_IOMMU_CONTROL 0x82000514
struct arm_smccc_res { long a0; };
struct iommu_fwspec { unsigned int num_ids; u32 ids[32]; };
struct device { void *data; struct iommu_fwspec *fwspec; };
struct clk_bulk_data { const char *id; void *clk; };
struct mtk_smi { unsigned int clk_num; struct clk_bulk_data clks[4]; void *smi_ao_base; };
struct of_device_id { const char *compatible; const void *data; };
static unsigned char regs[0x1000];
static unsigned int refs,reads,writes,offsets[64],clock_calls;
static int clock_error;
static u32 values[64];
static void *dev_get_drvdata(struct device *d) { return d->data; }
/* Model the kernel bit iterator without relying on host unsigned-long aliasing. */
#define for_each_set_bit(i,p,n) for ((i)=0;(i)<(n);(i)++) if (((const u8 *)(p))[(i)/8] & (1U<<((i)%8)))
static u32 readl(const void *p) {
    assert(refs==1 && (const u8 *)p>=regs && (const u8 *)p+4<=regs+sizeof(regs));
    u32 v; memcpy(&v,p,4); reads++; return v;
}
#define readl_relaxed readl
static void writel(u32 v,void *p) {
    assert(refs==1 && (u8 *)p>=regs && (u8 *)p+4<=regs+sizeof(regs));
    assert(writes<64); offsets[writes]=(u8 *)p-regs; values[writes++]=v; memcpy(p,&v,4);
}
#define writel_relaxed writel
static int clk_bulk_prepare_enable(unsigned int n,struct clk_bulk_data *clks) {
    assert(n==3 && clks && !refs); clock_calls++;
    if (clock_error) return clock_error;
    refs=1; return 0;
}
static void clk_bulk_disable_unprepare(unsigned int n,struct clk_bulk_data *clks) {
    assert(n==3 && clks && refs==1); refs=0;
}
static void arm_smccc_smc(unsigned long a,unsigned long b,unsigned long c,unsigned long d,
    unsigned long e,unsigned long f,unsigned long g,unsigned long h,struct arm_smccc_res *r)
{ (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;(void)h;(void)r; assert(!"unexpected secure call"); }
static bool dev_is_pci(struct device *dev) { (void)dev; assert(!"unexpected infra path"); return false; }
static int regmap_update_bits(void *map,unsigned int reg,u32 mask,u32 value)
{ (void)map;(void)reg;(void)mask;(void)value; assert(!"unexpected infra write"); return 0; }
static struct iommu_fwspec *dev_iommu_fwspec_get(struct device *dev) { return dev->fwspec; }
'''

MODEL = r'''
struct mtk_iommu_data {
    const struct mtk_iommu_plat_data *plat_data;
    struct mtk_smi_larb_iommu larb_imu[MTK_LARB_NR_MAX];
    struct device *dev;
    void *pericfg;
};
static void mtk_smi_larb_sleep_ctrl_disable(struct mtk_smi_larb *larb)
{ (void)larb; assert(!"unexpected sleep control"); }
static int mtk_smi_larb_sleep_ctrl_enable(struct mtk_smi_larb *larb)
{ (void)larb; assert(!"unexpected sleep control"); return 0; }
'''
MAIN = r'''
static const struct mtk_smi_larb_gen *match(const char *compat)
{
    for (unsigned int i=0;mtk_smi_larb_of_ids[i].compatible;i++)
        if (!strcmp(mtk_smi_larb_of_ids[i].compatible,compat)) return mtk_smi_larb_of_ids[i].data;
    assert(!"missing compatible"); return NULL;
}
static void fill(u32 seed) {
    assert(!refs); reads=writes=0;
    for (unsigned int off=0;off<sizeof(regs);off+=4) memcpy(regs+off,&seed,4);
}
static void verify(const unsigned char *expected)
{ assert(!memcmp(regs,expected,sizeof(regs))); }
static void run(struct mtk_smi_larb *larb,struct device *dev,u32 mask,u32 seed,bool trace)
{
    unsigned char expected[sizeof(regs)];
    fill(seed); memcpy(expected,regs,sizeof(regs));
    unsigned int count=0;
    for (unsigned int p=0;p<32;p++) if (mask & BIT(p)) {
        u32 value=seed|1; memcpy(expected+0x380+4*p,&value,4); count++;
    }
    assert(!mtk_smi_larb_resume(dev));
    verify(expected); assert(refs==1 && writes==count && reads==count);
    for (unsigned int i=0;i<writes;i++) {
        assert(values[i]==(seed|1) && offsets[i]>=0x380 && offsets[i]<0x400);
        assert(mask & BIT((offsets[i]-0x380)/4));
        if (i) assert(offsets[i]>offsets[i-1]);
        if (trace) printf("WRITE %u %u %u\n",larb->larbid,offsets[i],values[i]);
    }
    assert(!mtk_smi_larb_suspend(dev) && !refs); verify(expected);
}
int main(int argc,char **argv)
{
    assert(argc==1 || argc==4);
    struct mtk_iommu_data iommu={.plat_data=&mt6765_data};
    assert(mt6765_data.iova_region_nr==1 && mt6765_data.banks_num==1 && mt6765_data.banks_enable[0]);
    assert(mt6765_data.iova_region[0].iova_base==0 && mt6765_data.iova_region[0].size==SZ_4G-SZ_8M);
    struct mtk_smi_larb larb={.base=regs,.smi.clk_num=3};
    struct device dev={.data=&larb};
    struct iommu_fwspec fwspec={0}; struct device client={.fwspec=&fwspec};
    larb.larb_gen=match("mediatek,mt6765-smi-larb");
    assert(mtk_smi_larb_bind(&dev,NULL,iommu.larb_imu)==-ENODEV);
    unsigned int lo=0,hi=4;
    if (argc==4) { lo=strtoul(argv[1],NULL,0); hi=lo+1; assert(lo<4); }
    for (unsigned int l=lo;l<hi;l++) {
        memset(iommu.larb_imu,0,sizeof(iommu.larb_imu)); iommu.larb_imu[l].dev=&dev;
        assert(!mtk_smi_larb_bind(&dev,NULL,iommu.larb_imu));
        assert(larb.larbid==(int)l && larb.mmu==&iommu.larb_imu[l].mmu && larb.bank==iommu.larb_imu[l].bank);
        const u32 seeds[]={0,0xffffffff,0xa5c3b6d8};
        for (unsigned int n=0;n<(argc==4?1:37);n++) {
            u32 mask=n<32?BIT(n):n==32?0:n==33?0xffffffff:n==34?0xaaaaaaaa:n==35?0x55555555:7;
            if (argc==4) mask=strtoul(argv[2],NULL,0);
            fwspec.num_ids=0; *larb.mmu=0; memset(larb.bank,0xa5,32);
            for (unsigned int p=0;p<32;p++) if (mask & BIT(p))
                fwspec.ids[fwspec.num_ids++]=MTK_M4U_ID(l,p);
            /* IOMMU clients always have at least one ID; mask zero has no attach. */
            if (mask) assert(!mtk_iommu_config(&iommu,&client,true,0));
            assert(*larb.mmu==mask);
            for (unsigned int p=0;p<32;p++) assert(larb.bank[p]==((mask & BIT(p))?0:0xa5));
            for (unsigned int s=0;s<(argc==4?1:ARRAY_SIZE(seeds));s++) {
                u32 seed=argc==4?strtoul(argv[3],NULL,0):seeds[s];
                /* A failed bulk clock enable must perform no register accesses. */
                fill(seed); clock_error=-EIO;
                assert(mtk_smi_larb_resume(&dev)==-EIO && !refs && !reads && !writes);
                clock_error=0;
                run(&larb,&dev,mask,seed,argc==4);
                run(&larb,&dev,mask,seed,false); /* Restore after modeled power loss. */
            }
            /* Detach changes the software mask; shared gen2 doesn't clear an idle port. */
            if (mask) assert(!mtk_iommu_config(&iommu,&client,false,0));
            assert(!*larb.mmu);
            unsigned char old[sizeof(regs)]; memcpy(old,regs,sizeof(regs)); reads=writes=0;
            assert(!mtk_smi_larb_resume(&dev) && !reads && !writes); verify(old);
            assert(!mtk_smi_larb_suspend(&dev) && !refs);
        }
    }
    /* The old MT8167 and MT8173 bitmap registers retain their semantics. */
    const char *compat[]={"mediatek,mt8167-smi-larb","mediatek,mt8173-smi-larb"};
    for (unsigned int i=0;i<2;i++) {
        larb.larb_gen=match(compat[i]); *larb.mmu=0x12345; fill(0xa5a5a5a5);
        unsigned char expected[sizeof(regs)]; memcpy(expected,regs,sizeof(regs));
        memcpy(expected+(i?0xf00:0xfc0),larb.mmu,4);
        assert(!mtk_smi_larb_resume(&dev) && writes==1 && !reads); verify(expected);
        assert(!mtk_smi_larb_suspend(&dev) && !refs);
    }
    if (argc==1) puts("PASS: SMI bind, IOMMU masks/bank zero, per-port setup, clock errors, repeated resume and legacy bitmap backends");
}
'''


def block(s, name):
    match = re.search(r'^.*\b'+re.escape(name)+r'(?:\(|\s*\{|\s*(?:\[.*?\])?\s*=)', s, re.M)
    assert match, name
    start = match.start()
    if s[start:].startswith(name+'('): start = s.rfind('\n', 0, start-1)+1
    end = s.index('\n}', start)+2
    if s[end:end+1] == ';': end += 1
    return s[start:end]+'\n'


def build_harness(source):
    s = source.read_text(); iommu = (SRC/'drivers/iommu/mtk_iommu.c').read_text()
    smih = (SRC/'include/soc/mediatek/smi.h').read_text()
    code = PRELUDE+s[s.index('/* SMI COMMON */'):s.index('struct mtk_smi_common_plat')]
    # Clock name arrays are unused in this callback harness.
    code = code[:code.index('static const char * const mtk_smi_larb_clks')]
    code += block(s, 'mtk_smi_larb_gen')+block(s, 'mtk_smi_larb')
    code += block(smih, 'iommu_atf_cmd')+block(smih, 'mtk_smi_larb_iommu')
    code += iommu[iommu.index('#define HAS_4GB_MODE'):iommu.index('enum mtk_iommu_plat')]
    for name in ('REG_MMU_INV_SEL_GEN1', 'PERICFG_IOMMU_1'):
        code += re.search(r'^#define '+name+r'\s+[^\n]+', iommu, re.M).group()+'\n'
    for name in ('mtk_iommu_plat', 'mtk_iommu_iova_region', 'mtk_iommu_plat_data', 'single_domain', 'mt6765_data'):
        code += block(iommu, name)
    code += MODEL+block(iommu, 'mtk_iommu_config')
    for name in ('mtk_smi_larb_bind', 'mtk_smi_larb_config_port_gen1',
                 'mtk_smi_larb_config_port_mt8167', 'mtk_smi_larb_config_port_mt8173',
                 'mtk_smi_larb_config_port_gen2_general'):
        code += block(s, name)
    start = s.index('static const u8 mtk_smi_larb_mt6893_ostd')
    end = s.index('MODULE_DEVICE_TABLE(of, mtk_smi_larb_of_ids)')
    code += s[start:end]+block(s, 'mtk_smi_larb_resume')+block(s, 'mtk_smi_larb_suspend')+MAIN
    path = ROOT/'out/mt6765-smi-host.c'; path.write_text(code)
    binary = ROOT/'out/mt6765-smi-host'
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
        '-Wno-sign-compare', '-fsanitize=address,undefined', '-fno-pie', '-no-pie', '-O1', '-g',
        '-I'+str(SRC/'include'), str(path), '-o', str(binary)], check=True)
    return binary


def check_dtb(path):
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    nodes = module.fdt_nodes(path.read_bytes())
    phandles = {struct.unpack('>I', v['phandle'])[0]:v for v in nodes.values() if 'phandle' in v}
    addresses = ['14003000', '17010000', '15021000', '1a002000']
    iommu = nodes['/soc/m4u@10205000']
    assert iommu['mediatek,larbs'] == b''.join(nodes['/soc/memory-controller@'+a]['phandle'] for a in addresses)
    for i, addr in enumerate(addresses):
        n = nodes['/soc/memory-controller@'+addr]
        assert n['compatible'] == b'mediatek,mt6765-smi-larb\0'
        assert struct.unpack('>I', n['mediatek,larb-id'])[0] == i
        names = n['clock-names'].rstrip(b'\0').split(b'\0')
        assert names == [b'apb', b'smi']
        clocks = list(struct.unpack('>'+str(len(n['clocks'])//4)+'I', n['clocks']))
        count = 0
        while clocks:
            provider = phandles[clocks.pop(0)]
            cells = struct.unpack('>I', provider['#clock-cells'])[0]
            assert len(clocks) >= cells
            del clocks[:cells]; count += 1
        assert count == len(names)
    for addr, port in [('1400b000',0), ('1400c000',1), ('1400d000',2)]:
        name = ('rdma' if port == 2 else 'ovl')+'@'+addr
        assert nodes['/soc/'+name]['iommus'] == iommu['phandle']+struct.pack('>I', port)
    return dict(larbs=addresses, display_ports=[0,1,2])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'drivers/memory/mtk-smi.c')
    parser.add_argument('--dtb', type=Path, default=ROOT/'dist/mainline/mt6765-rabbit-r1.dtb')
    args = parser.parse_args()
    for p in vars(args).values():
        if not p.resolve().is_relative_to(ROOT): raise SystemExit('Inputs must be under /rabbitr1')
    binary = build_harness(args.source)
    subprocess.run([str(binary)], check=True)
    dtb = check_dtb(args.dtb)
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    ports = stock_ports(raw)
    traces = []
    for seed in (0, 0xffffffff, 0xa5c3b6d8):
        stock = stock_config(raw, ports, seed); traces.extend(stock)
        for larb in range(4):
            mask = sum(1<<p['port'] for p in ports if p['larb'] == larb)
            output = subprocess.check_output([str(binary), str(larb), str(mask), str(seed)], text=True)
            observed = [tuple(map(int, line.split()[1:])) for line in output.splitlines()]
            expected = [(larb, t['writes'][0][0]-0x1000*larb, t['writes'][0][1])
                        for t in stock if t['larb'] == larb and t['virtual']]
            assert observed == expected, (observed, expected)
    (ROOT/'out/mt6765-smi-audit.json').write_text(json.dumps(dict(
        stock_function='m4u_config_port', raw_offset='0x8a13cc', ports=ports,
        traces=traces, dtb=dtb, hardware_tested=False), indent=2)+'\n')
    print(f'PASS: {len(traces)} stock port enable/disable traces; native enable matches all 52 ports across three seeds')
    print('PASS: compiled native larbs, clock counts and display IOMMU IDs')
    print('No DMA, address translation, TLB, firmware handoff or physical clocks are emulated.')


if __name__ == '__main__': main()
