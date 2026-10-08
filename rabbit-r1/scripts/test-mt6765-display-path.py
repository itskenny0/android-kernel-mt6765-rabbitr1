#!/usr/bin/env python3
"""Compare MT6765 display routing/mutex writes with selected stock instructions."""
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
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--routes', type=Path, default=SRC/'drivers/soc/mediatek/mt6765-mmsys.h')
parser.add_argument('--mutex', type=Path, default=SRC/'drivers/soc/mediatek/mtk-mutex.c')
parser.add_argument('--dtb', type=Path, default=ROOT/'dist/mainline/mt6765-rabbit-r1.dtb')
args = parser.parse_args()
for path in vars(args).values():
    if not path.resolve().is_relative_to(ROOT):
        raise SystemExit('Inputs must be under /rabbitr1')

# Chosen DRM path, expanded by the vendor's two virtual routing nodes. This is
# an input fixture for the shipped routing code, NOT its default RSZ pipeline.
STOCK_PATH = [0, 1, 3, 25, 26, 8, 9, 10, 11, 12, 13, -1]
FIELDS = {0xf3c: 2, 0xf40: 1, 0xf48: 1, 0xf4c: 3, 0xf50: 1,
          0xf54: 3, 0xf60: 1, 0xf30: 1, 0xf68: 1}
LK_FIELDS = {address: mask for address, mask in FIELDS.items() if address != 0xf30} | {0xf64: 1}
NATIVE_FIELDS = FIELDS | LK_FIELDS
MOUT = {0xf3c, 0xf40, 0xf50}


class Stock:
    def __init__(self, raw, seed):
        self.bias, self.obj, self.mmio, self.stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
        self.uc = uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        uc.mem_map(self.bias, 0x2000000); uc.mem_write(self.bias, raw)
        uc.mem_map(self.obj, 0x10000); uc.mem_map(self.mmio, 0x2000); uc.mem_map(self.stop, 0x1000)
        uc.mem_write(self.mmio, struct.pack('<I', seed)*0x800)
        path = struct.pack('<24i', *STOCK_PATH, *([-1]*(24-len(STOCK_PATH))))
        uc.mem_write(self.obj, path)
        uc.mem_write(self.bias+0x17822a0, path)  # scenario 0, used by ddp_mutex_set
        self.events = []
        self.pointers = ({0x17826a8+88*i for i in range(4)} |
                         {0x17827e8+56*i for i in range(2)} |
                         {0x1782858+56*i for i in range(6)})
        self.cache = {0x17826b0+88*i for i in range(4)}
        uc.hook_add(UC_HOOK_CODE, self.code)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)
        self.call(0x72d22c)  # actual ddp_path_init installs register pointers
        assert not self.events

    def code(self, uc, address, size, _):
        at = address-self.bias
        if at in (0x1f01c, 0x747728, 0xa5e2c, 0x420ad0, 0x743f94):
            if at == 0x743f94:
                module = uc.reg_read(UC_ARM64_REG_X0)
                assert module in (17, 18), module
                uc.reg_write(UC_ARM64_REG_X0, self.mmio+(0x1000 if module == 18 else 0))
            elif at != 0x1f01c:
                uc.reg_write(UC_ARM64_REG_X0, 0)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            allowed = ((0x72d22c, 0x72d324), (0x72d440, 0x72d480),
                       (0x72dae8, 0x72e31c), (0x72e400, 0x72e76c),
                       (0x7306d8, 0x730ca0), (0x731074, 0x7312a0),
                       (0x732548, 0x732564), (0x74390c, 0x743934),
                       (0x74398c, 0x743aa8), (0x72f7b0, 0x72fa10))
            assert any(lo <= at < hi for lo, hi in allowed), hex(at)

    def write(self, uc, access, address, size, value, _):
        if self.mmio <= address < self.mmio+0x2000:
            assert size == 4
            self.events.append((address-self.mmio, value))
        elif not self.obj+0x1000 <= address < self.obj+0x10000:
            at = address-self.bias
            assert (size == 8 and at in self.pointers) or (size == 4 and at in self.cache), hex(address)

    def call(self, offset, *args):
        uc = self.uc
        uc.reg_write(UC_ARM64_REG_SP, self.obj+0x8000)
        uc.reg_write(UC_ARM64_REG_X30, self.stop)
        for reg, value in zip((UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3),
                              list(args)+[0]*4):
            uc.reg_write(reg, value)
        start = len(self.events)
        uc.emu_start(self.bias+offset, self.stop, count=30000)
        assert uc.reg_read(UC_ARM64_REG_PC) == self.stop
        assert uc.reg_read(UC_ARM64_REG_SP) == self.obj+0x8000
        return self.events[start:]

    def routes(self, connect):
        return self.call(0x72dae8 if connect else 0x72e400, self.obj, 0)

    def mutex(self, index):
        return self.call(0x7306d8, index, 0, 0, 0)


prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define WARN_ON(c) assert(!(c))
#define fallthrough __attribute__((fallthrough))
#define pr_debug(...) ((void)0)
#define EXPORT_SYMBOL_GPL(x)
struct cmdq_pkt { int unused; };
struct cmdq_client_reg { u32 size,subsys,offset; };
struct device { void *data; };
static void *dev_get_drvdata(struct device *d) { return d->data; }
static int cmdq_pkt_write_mask(struct cmdq_pkt *p,u32 s,u32 o,u32 v,u32 m)
{ (void)p; (void)s; (void)o; (void)v; (void)m; assert(false); return 0; }
static u8 regs[0x2000];
static bool trace=true;
static u32 readl_relaxed(const void *p)
{ assert((const u8 *)p>=regs && (const u8 *)p+4<=regs+sizeof(regs)); u32 v; memcpy(&v,p,4); return v; }
static void writel_relaxed(u32 v,void *p)
{ (void)readl_relaxed(p); memcpy(p,&v,4); if (trace) printf("W %zx %x\n",(u8 *)p-regs,v); }
static void fill(u32 seed) { for (size_t i=0;i<sizeof(regs);i+=4) memcpy(regs+i,&seed,4); }
'''
models = r'''
struct mtk_mmsys { void *regs; const struct mtk_mmsys_driver_data *data; struct cmdq_client_reg cmdq_base; };
struct mtk_mutex_ctx { void *regs; struct mtk_mutex mutex[MTK_MUTEX_MAX_HANDLES]; const struct mtk_mutex_data *data; };
'''
main = r'''
static void legacy(void)
{
    trace=false;
    for (unsigned int n=0;n<MTK_MUTEX_MAX_HANDLES;n++) {
        struct mtk_mutex_ctx m={ .regs=regs+0x1000,.data=&mt8183_mutex_driver_data };
        m.mutex[n].id=n;
        fill(0xa5000080);
        mtk_mutex_add_comp(&m.mutex[n],DDP_COMPONENT_DSI0);
        assert(readl_relaxed(regs+0x1030+32*n)==0xa5000080);
        assert(readl_relaxed(regs+0x102c+32*n)==0x41);
        mtk_mutex_add_comp(&m.mutex[n],DDP_COMPONENT_OVL0);
        mtk_mutex_add_comp(&m.mutex[n],DDP_COMPONENT_RDMA0);
        assert(readl_relaxed(regs+0x1030+32*n)==0xa5000281);
        mtk_mutex_remove_comp(&m.mutex[n],DDP_COMPONENT_DSI0);
        assert(readl_relaxed(regs+0x1030+32*n)==0xa5000281);
        assert(readl_relaxed(regs+0x102c+32*n)==0);
        mtk_mutex_remove_comp(&m.mutex[n],DDP_COMPONENT_OVL0);
        mtk_mutex_remove_comp(&m.mutex[n],DDP_COMPONENT_RDMA0);
        assert(readl_relaxed(regs+0x1030+32*n)==0xa5000080);
        m.data=&mt2712_mutex_driver_data;
        fill(0xa5000080);
        mtk_mutex_add_comp(&m.mutex[n],DDP_COMPONENT_AAL1);
        assert(readl_relaxed(regs+0x1034+32*n)==0xa5000082);
        assert(readl_relaxed(regs+0x102c+32*n)==0xa5000080);
        mtk_mutex_remove_comp(&m.mutex[n],DDP_COMPONENT_AAL1);
        assert(readl_relaxed(regs+0x1034+32*n)==0xa5000080);
    }
    trace=true;
}
int main(int argc,char **argv)
{
    assert(argc==2); u32 seed=strtoul(argv[1],NULL,0);
    legacy();
    struct mtk_mmsys s={ .regs=regs,.data=&mt6765_mmsys_driver_data };
    struct device dev={ .data=&s };
    fill(seed);
    for (unsigned int phase=0;phase<3;phase++) {
        printf("ROUTE %u\n",phase);
        for (unsigned int i=1;i<ARRAY_SIZE(path);i++)
            if (phase==1) mtk_mmsys_ddp_disconnect(&dev,path[i-1],path[i]);
            else mtk_mmsys_ddp_connect(&dev,path[i-1],path[i]);
    }
    for (unsigned int n=0;n<MTK_MUTEX_MAX_HANDLES;n++) {
        struct mtk_mutex_ctx m={ .regs=regs+0x1000,.data=&mt6765_mutex_driver_data };
        m.mutex[n].id=n;
        fill(seed); printf("MUTEX %u\n",n);
        for (unsigned int i=0;i<ARRAY_SIZE(path);i++) mtk_mutex_add_comp(&m.mutex[n],path[i]);
        puts("REMOVE 0");
        for (size_t i=ARRAY_SIZE(path);i>0;i--) mtk_mutex_remove_comp(&m.mutex[n],path[i-1]);
    }
}
'''


def block(source, start):
    begin = source.index(start)
    end = source.index('\n}', begin)+2
    if source[end:end+1] == ';': end += 1
    return source[begin:end]+'\n'


def harness():
    soc = SRC/'drivers/soc/mediatek'
    mutex = args.mutex.read_text()
    mmsys = (soc/'mtk-mmsys.c').read_text()
    header = (soc/'mtk-mmsys.h').read_text()
    drm = (SRC/'drivers/gpu/drm/mediatek/mtk_drm_drv.c').read_text()
    data = block(drm, 'static const struct mtk_mmsys_driver_data mt6765_mmsys_driver_data')
    name = re.search(r'\.main_path = (\w+)', data)[1]
    path = block(drm, 'static const unsigned int '+name+'[]')
    expected = ['OVL0','OVL_2L0','RDMA0','COLOR0','CCORR','AAL0','GAMMA','DITHER0','DSI0']
    assert re.findall(r'DDP_COMPONENT_(\w+)', path) == expected
    path = path.replace(name, 'path')
    text = prelude
    text += block((SRC/'include/linux/soc/mediatek/mtk-mmsys.h').read_text(), 'enum mtk_ddp_comp_id {')
    text += block((SRC/'include/linux/soc/mediatek/mtk-mutex.h').read_text(), 'enum mtk_mutex_mod_index {')
    text += mutex[mutex.index('#define MTK_MUTEX_MAX_HANDLES'):mutex.index('struct mtk_mutex_ctx')]
    text += block(header, 'struct mtk_mmsys_routes {')
    text += block(header, 'struct mtk_mmsys_driver_data {')
    text += models + args.routes.read_text() + path
    for platform in ['mt6765', 'mt8183', 'mt2712']:
        text += block(mutex, 'static const u8 '+platform+'_mutex_mod[')
    text += block(mutex, 'static const u8 mt8183_mutex_table_mod[')
    for platform in ['mt8183', 'mt2712']:
        text += block(mutex, 'static const u16 '+platform+'_mutex_sof[')
    for platform in ['mt6765', 'mt8183', 'mt2712']:
        text += block(mutex, 'static const struct mtk_mutex_data '+platform+'_mutex_driver_data')
    # Actual header constants, not duplicated numeric reset/unused mixer values.
    for file, token in [('mt8183-mmsys.h','MT8183_MMSYS_SW0_RST_B'),
                        ('mt8188-mmsys.h','MT8188_VDO1_MIXER_VSYNC_LEN')]:
        text += re.search(r'^#define '+token+r'\s+[^\n]+', (soc/file).read_text(), re.M)[0]+'\n'
    text += block(mmsys, 'static const struct mtk_mmsys_driver_data mt6765_mmsys_driver_data')
    for name in ['static void mtk_mmsys_update_bits(', 'void mtk_mmsys_ddp_connect(',
                 'void mtk_mmsys_ddp_disconnect(']: text += block(mmsys, name)
    for name in ['static void mtk_mutex_update_mod(', 'void mtk_mutex_add_comp(',
                 'void mtk_mutex_remove_comp(']: text += block(mutex, name)
    return text+main


def compiled_clocks():
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec); spec.loader.exec_module(validate)
    nodes = validate.fdt_nodes(args.dtb.read_bytes())
    stock = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    def cells(value): return struct.unpack('>'+'I'*(len(value)//4), value)
    def provider(tree, phandle): return next(n for n in tree.values() if n.get('phandle') == struct.pack('>I', phandle))
    names = stock['/dispsys']['clock-names'].split(b'\0')[:-1]
    old = cells(stock['/dispsys']['clocks'])
    selected = old[names.index(b'MMSYS_DISP_RDMA0')*2:][:2]
    new = cells(nodes['/soc/rdma@1400d000']['clocks'])
    assert len(new) == 2 and new[1] == selected[1] == 10, (new, selected)
    for tree, clock, compatible in [(nodes, new, b'mediatek,mt6765-mmsys'),
                                    (stock, selected, b'mediatek,mt6765-mmsys_config')]:
        p = provider(tree, clock[0])
        assert p['compatible'].split(b'\0')[:-1] == [compatible, b'syscon']
        assert cells(p['reg']) == (0, 0x14000000, 0, 0x1000)
        assert cells(p['#clock-cells']) == (1,)
    bindings = (SRC/'include/dt-bindings/clock/mt6765-clk.h').read_text()
    ident = re.search(r'^#define\s+CLK_MM_DISP_RDMA0\s+(\d+)', bindings, re.M)
    assert int(ident[1]) == new[1]
    gate = (SRC/'drivers/clk/mediatek/clk-mt6765-mm.c').read_text()
    assert re.search(r'GATE_MM\(CLK_MM_DISP_RDMA0,\s*"mm_disp_rdma0",\s*"mm_ck",\s*10\)', gate)
    print('PASS: compiled RDMA0 clock matches stock MMSYS provider, ID and gate bit 10')


def check():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    spec = importlib.util.spec_from_file_location('lk_routes', Path(__file__).with_name('test-lk-display-path.py'))
    lk_routes = importlib.util.module_from_spec(spec); spec.loader.exec_module(lk_routes)
    lk_routes.check()
    lk_raw = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    out = ROOT/'out/display-path-test'; out.mkdir(exist_ok=True)
    source = out/'harness.c'; binary = out/'harness'
    source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-sign-compare',
                    '-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)], check=True)
    audit = []
    for seed in [0, 0xffffffff, 0xa5a5a5a5, 0x5a5a5a5a]:
        result = subprocess.check_output([str(binary), str(seed)], text=True)
        sections = {}; current = None; number = None
        for line in result.splitlines():
            if line.startswith('W '):
                sections[current].append(tuple(int(v, 16) for v in line.split()[1:]))
            else:
                kind, value = line.split()
                if kind == 'MUTEX': number = int(value)
                current = (kind, number if kind == 'REMOVE' else int(value))
                assert current not in sections
                sections[current] = []
        old = Stock(raw, seed)
        lk = lk_routes.StockLK(lk_raw, seed)
        case = {'initial_register_value': seed, 'route_phases': [], 'mutexes': []}
        for phase in range(3):
            stock_writes = old.routes(phase != 1)
            lk_writes = lk.routes(phase != 1)
            reference = dict(stock_writes)
            lk_reference = dict(lk_writes)
            actual = dict(sections['ROUTE', phase])
            case['route_phases'].append({'phase': phase, 'stock': stock_writes, 'lk': lk_writes,
                                        'mainline': sections['ROUTE', phase]})
            assert len(sections['ROUTE', phase]) == len(NATIVE_FIELDS) and set(actual) == set(NATIVE_FIELDS)
            if phase != 1:
                assert set(reference) == set(FIELDS), reference
                assert set(lk_reference) == set(LK_FIELDS), lk_reference
                for address in set(reference) & set(lk_reference):
                    assert reference[address] == lk_reference[address]
            else:
                assert set(reference) == MOUT, reference
                assert set(lk_reference) == MOUT, lk_reference
            reference |= lk_reference
            for address, mask in NATIVE_FIELDS.items():
                value = reference[address] & mask if phase != 1 else 0
                assert actual[address] == (seed & ~mask) | value, (seed,phase,hex(address),actual,reference)
        for n in range(10):
            writes = sections['MUTEX', n]
            final = dict(writes); mod, sof = 0x1030+n*32, 0x102c+n*32
            assert set(final) == {mod, sof} and len(writes) == 10
            assert final[mod] == seed | 0x1fb80 and final[sof] == 0x41
            if n < 4:
                stock_writes = old.mutex(n)
                reference = dict(stock_writes)
                assert reference[mod] == 0x1fb80 and reference[sof] == final[sof]
                removed = old.call(0x72f7b0, n, 13, 0)
                assert removed == [(mod, 0xfb80)], removed
                case['mutexes'].append({'id': n, 'stock': stock_writes, 'mainline': writes,
                                       'stock_dsi_removal': removed})
            removed = sections['REMOVE', n]
            assert len(removed) == 10 and removed[:2] == [(mod, (seed | 0x1fb80) & ~0x10000), (sof, 0)]
            assert dict(removed) == {mod: seed & ~0x1fb80, sof: 0}
        audit.append(case)
    print('PASS: stock Android and LK routing, mutex comparison; four initial states, reconnect/cleanup, ten mutex IDs')
    print('PASS: production callbacks under ASan/UBSan; MT8183 DSI/OVL/RDMA and MT2712 MOD1 behavior retained')
    compiled_clocks()
    (ROOT/'out/mt6765-display-path-audit.json').write_text(json.dumps({
        'stock_image_sha256': hashlib.sha256(raw).hexdigest(),
        'lk_sha256': lk_routes.SHA256,
        'custom_stock_path': STOCK_PATH, 'cases': audit,
    }, indent=2)+'\n')
    print('No CMDQ, physical display, frame synchronization or clock waveform is emulated.')


if __name__ == '__main__':
    check()
