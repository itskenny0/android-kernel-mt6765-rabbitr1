#!/usr/bin/env python3
"""Check native RDMA setup against stock instructions, with MMIO/clock models."""
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
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/gpu/drm/mediatek/mtk_disp_rdma.c')
parser.add_argument('--dtb', type=Path, default=ROOT/'dist/mainline/mt6765-rabbit-r1.dtb')
args = parser.parse_args()
for path in vars(args).values():
    if not path.resolve().is_relative_to(ROOT): raise SystemExit('Inputs must be under /rabbitr1')

GOLDEN_REGS = [0x30,0x34,0x3c,0x40,0xa8,0xac,0xc0,0xd0,0xd4,0xd8,0xdc,0xe8,0xb0]


def stock(raw, width, height, bpc, high, memory, seed, reset=False, latency=0):
    bias,obj,mmio,stop = 0x1000000,0x20000000,0x30000000,0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias,0x2000000); uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000); uc.mem_map(mmio,0x1000); uc.mem_map(stop,0x1000)
    # Published golden_setting_context: no RSZ/WROT borrowing, video mode.
    context = [1,0,0,high,0,0,0,memory,0,0,width,height,0,0,60,0,width,height]
    uc.mem_write(obj,struct.pack('<18I',*context))
    uc.mem_write(mmio,struct.pack('<I',seed)*0x400)
    uc.mem_write(mmio+0x10,struct.pack('<I',(seed & ~0x711)|0x100))
    for reg,value in [(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop),
                      (UC_ARM64_REG_X0,3 if reset else 0),(UC_ARM64_REG_X1,0 if reset else bpc*3),
                      (UC_ARM64_REG_X2,0),(UC_ARM64_REG_X3,obj)]: uc.reg_write(reg,value)
    events=[]; pending=None; remaining=0; delays=[]
    def code(uc,address,size,_):
        at=address-bias
        if at in (0x1f01c,0xa5e2c,0x420ad0,0x747728,0x732548,0x743f94,0x764340,0xf1dee0):
            if at==0x743f94:
                module=uc.reg_read(UC_ARM64_REG_X0)
                assert module in (3,4),module
                uc.reg_write(UC_ARM64_REG_X0,mmio+(module-3)*0x1000)
            elif at==0xf1dee0:
                ticks=uc.reg_read(UC_ARM64_REG_X0)
                assert ticks==42950
                delays.append(10)
            elif at!=0x1f01c:
                uc.reg_write(UC_ARM64_REG_X0,1 if at==0x764340 else 0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert ((0x7034a4<=at<0x7037bc or 0x7024f4<=at<0x702584)
                    if reset else 0x7037bc<=at<0x7055c0),hex(at)
    def write(uc,access,address,size,value,_):
        nonlocal pending,remaining
        if mmio<=address<mmio+0x1000:
            assert size==4
            events.append((address-mmio,value))
            if reset and address==mmio+0x10:
                pending=0 if value&16 else 0x100; remaining=latency
        else:
            assert obj+0x1000<=address<obj+0x10000 or (address==bias+0x19f8b00 and size==8),hex(address)
    def read(uc,access,address,size,value,_):
        nonlocal pending,remaining
        if reset and address==mmio+0x10 and pending is not None:
            if remaining:
                remaining-=1
            else:
                v=struct.unpack('<I',uc.mem_read(address,4))[0]
                uc.mem_write(address,struct.pack('<I',(v&~0x700)|pending)); pending=None
    uc.hook_add(UC_HOOK_CODE,code); uc.hook_add(UC_HOOK_MEM_WRITE,write); uc.hook_add(UC_HOOK_MEM_READ,read)
    uc.emu_start(bias+(0x7034a4 if reset else 0x7037bc),stop,count=30000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert bytes(uc.mem_read(obj,72))==struct.pack('<18I',*context)
    if reset:
        assert uc.reg_read(UC_ARM64_REG_X0)==0 and pending is None
        assert delays==[10]*(latency*2),delays
    else:
        assert [address for address,value in events]==GOLDEN_REGS,events
    return events


def block(source,start):
    begin=source.index(start); end=source.index('\n}',begin)+2
    if source[end:end+1]==';': end+=1
    return source[begin:end]+'\n'


PRELUDE=r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t s64;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define U32_MAX UINT32_MAX
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define SZ_1K 1024
#define SZ_4K (4*SZ_1K)
#define SZ_8K (8*SZ_1K)
#define div_u64(a,b) ((u64)(a)/(b))
#define DIV_ROUND_UP_ULL(a,b) (((u64)(a)+(b)-1)/(b))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define max_t(t,a,b) ((t)(a)>(t)(b)?(t)(a):(t)(b))
#define check_mul_overflow(a,b,r) __builtin_mul_overflow((a),(b),(r))
#define dev_err(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
struct clk { unsigned long rate; };
struct cmdq_client_reg { int unused; };
struct device { void *data; void *of_node; };
struct of_device_id { const char *compatible; const void *data; };
struct mtk_plane_pending_state { u32 addr,pitch,format; };
struct mtk_plane_state { struct mtk_plane_pending_state pending; };
static void *dev_get_drvdata(struct device *d) { return d->data; }
static unsigned char regs[0x1000];
static unsigned int writes,reads,clock_refs,clock_calls,polls;
static unsigned int irq_depth=1,irq_enables,irq_disables;
static bool irq_inflight;
static void (*irq_before_disable)(void);
static u32 late_status;
static int atomic_context,clock_error,reset_error,latency,pending=-1,remaining;
static bool trace;
static u32 load(unsigned int offset) { u32 v; memcpy(&v,regs+offset,4); return v; }
static void save(unsigned int offset,u32 value) { memcpy(regs+offset,&value,4); }
static u32 readl(const void *p)
{
    assert(clock_refs && (const u8 *)p>=regs && (const u8 *)p+4<=regs+sizeof(regs));
    reads++;
    unsigned int off=(const u8 *)p-regs;
    if (off==0x10 && pending>=0) {
        if (remaining) remaining--;
        else if (!((reset_error==1 && !pending) || (reset_error==2 && pending))) {
            save(off,(load(off)&~0x700)|(unsigned int)pending); pending=-1;
        }
    }
    u32 value=load(off);
    if (off==4 && late_status) { save(off,value|late_status); late_status=0; }
    return value;
}
static void writel(u32 value,void *p)
{
    assert(clock_refs && (u8 *)p>=regs && (u8 *)p+4<=regs+sizeof(regs));
    unsigned int off=(u8 *)p-regs;
    if (off==0x10 && ((load(off)^value)&16)) {
        pending=value&16?0:0x100; remaining=latency;
    }
    save(off,off==4?load(off)&value:value); writes++;
    if (trace) printf("W %x %x\n",off,value);
}
#define readl_poll_timeout(addr,val,cond,delay,timeout) ({ \
    assert(!atomic_context && (delay)==10 && (timeout)==100000); \
    int ret=-ETIMEDOUT; \
    for (int i=0;i<8;i++) { polls++; (val)=readl(addr); if (cond) { ret=0; break; } } \
    ret; })
static int clk_prepare_enable(struct clk *c)
{ (void)c; assert(!atomic_context); clock_calls++; if (clock_error) return clock_error; clock_refs++; return 0; }
static void clk_disable_unprepare(struct clk *c)
{ (void)c; assert(!atomic_context && clock_refs && !irq_inflight); clock_refs--; }
static unsigned long clk_get_rate(struct clk *c)
{ assert(!atomic_context && clock_refs); return c->rate; }
static void enable_irq(int irq)
{
    (void)irq; assert(!atomic_context && clock_refs && irq_depth==1 && !load(0));
    assert((load(0x10)&0x711)==0x100);
    irq_depth--; irq_enables++;
}
static void disable_irq(int irq)
{
    (void)irq; assert(!atomic_context && clock_refs && !irq_depth && !load(0));
    irq_depth++; irq_disables++;
    if (irq_before_disable) irq_before_disable();
    irq_inflight=false;
}
struct queued { u32 offset,value,mask; };
struct cmdq_pkt { struct queued ops[128]; size_t count; };
static void queue(struct cmdq_pkt *pkt,u32 v,void *base,u32 offset,u32 mask)
{
    assert(base==regs && offset<sizeof(regs));
    if (pkt) { assert(pkt->count<128); pkt->ops[pkt->count++]=(struct queued){offset,v,mask}; }
    else writel((readl(regs+offset)&~mask)|(v&mask),regs+offset);
}
static void mtk_ddp_write(struct cmdq_pkt *p,u32 v,struct cmdq_client_reg *c,void *base,u32 off)
{ (void)c; queue(p,v,base,off,~0U); }
static void mtk_ddp_write_relaxed(struct cmdq_pkt *p,u32 v,struct cmdq_client_reg *c,void *base,u32 off)
{ mtk_ddp_write(p,v,c,base,off); }
static void mtk_ddp_write_mask(struct cmdq_pkt *p,u32 v,struct cmdq_client_reg *c,void *base,u32 off,u32 mask)
{ (void)c; queue(p,v,base,off,mask); }
static void flush(struct cmdq_pkt *p)
{ for (size_t i=0;i<p->count;i++) queue(NULL,p->ops[i].value,regs,p->ops[i].offset,p->ops[i].mask); p->count=0; }
'''
MAIN=r'''
static void fill(u32 seed)
{ for (unsigned int i=0;i<sizeof(regs);i+=4) save(i,seed); save(0x10,(seed&~0x711)|0x100); pending=-1; }
static const struct mtk_disp_rdma_data *match(const char *name)
{
    for (const struct of_device_id *p=mtk_disp_rdma_driver_dt_match;p->compatible;p++)
        if (!strcmp(name,p->compatible)) return p->data;
    assert(false); return NULL;
}
static void lifecycle(void)
{
    struct clk clk={230000000};
    struct mtk_disp_rdma rdma={.clk=&clk,.regs=regs,.data=match("mediatek,mt6765-disp-rdma")};
    struct device dev={.data=&rdma};
    for (int fail=0;fail<3;fail++) {
        fill(0xa5000000); writes=0; polls=0; reset_error=fail; latency=2;
        int ret=mtk_rdma_clk_enable(&dev);
        assert(ret==(fail?-ETIMEDOUT:0));
        assert(!(load(0x10)&0x11));
        if (fail) { assert(irq_depth==1 && !clock_refs && !rdma.config_valid && !rdma.clock_rate); continue; }
        assert(clock_refs==1 && (load(0x10)&0x700)==0x100 && polls==6);
        unsigned int before=writes;
        mtk_rdma_start(&dev); assert(writes==before);
        atomic_context=1;
        mtk_rdma_config(&dev,480,640,60,8,NULL);
        assert(rdma.config_valid);
        mtk_rdma_start(&dev); assert(load(0x10)&1);
        mtk_rdma_stop(&dev); assert(!rdma.config_valid && !(load(0x10)&1));
        assert(!load(0) && !load(4));
        atomic_context=0; mtk_rdma_clk_disable(&dev); assert(!clock_refs && !rdma.clock_rate);
    }
    reset_error=0; latency=0;
    for (unsigned int n=0;n<3;n++) {
        clk.rate=n==0?0:n==1?0x100000000UL:230000000;
        clock_error=n==2?-EIO:0; fill(0); writes=0;
        assert(mtk_rdma_clk_enable(&dev)==(clock_error?-EIO:-EINVAL));
        assert(irq_depth==1 && !clock_refs && !rdma.clock_rate && !rdma.config_valid);
        unsigned int before=writes;
        atomic_context=1; mtk_rdma_config(&dev,480,640,60,8,NULL); mtk_rdma_start(&dev);
        atomic_context=0; assert(writes==before);
    }
    clock_error=0; clk.rate=230000000; fill(0); assert(!mtk_rdma_clk_enable(&dev));
    const unsigned int bad[][4]={{0,640,60,8},{8192,1,60,8},{480,0,60,8},
        {1,1048576,60,8},{480,640,0,8},{480,640,60,5},{480,640,60,11},
        {8191,1048575,UINT32_MAX,10},{3840,2160,240,8}};
    for (size_t i=0;i<ARRAY_SIZE(bad);i++) {
        atomic_context=1; mtk_rdma_config(&dev,480,640,60,8,NULL); mtk_rdma_start(&dev);
        mtk_rdma_config(&dev,bad[i][0],bad[i][1],bad[i][2],bad[i][3],NULL);
        assert(!rdma.config_valid && !(load(0x10)&1));
        unsigned int before=writes; mtk_rdma_start(&dev); assert(writes==before);
        atomic_context=0;
    }
    rdma.clock_rate=1000000;
    struct mt6765_rdma_fifo f;
    assert(mt6765_rdma_calc_fifo(&rdma,480,640,60,8,false,&f)==-ERANGE);
    rdma.clock_rate=230000000;
    atomic_context=1; mtk_rdma_config(&dev,480,640,60,6,NULL);
    assert(rdma.config_valid && rdma.bpc==6);
    mtk_rdma_config(&dev,480,640,60,0,NULL); assert(rdma.config_valid && rdma.bpc==6);
    atomic_context=0; mtk_rdma_clk_disable(&dev);
    rdma.data=match("mediatek,mt8183-disp-rdma"); fill(0); writes=0;
    assert(!mtk_rdma_clk_enable(&dev) && !writes);
    atomic_context=1; mtk_rdma_config(&dev,480,640,60,8,NULL);
    assert(load(0x40)==0x814000e0 && writes==3);
    mtk_rdma_start(&dev); assert(load(0x10)&1); mtk_rdma_stop(&dev); assert(!(load(0x10)&1));
    struct mtk_plane_state plane={.pending={.addr=0x43210000,.pitch=1920,.format=DRM_FORMAT_XRGB8888}};
    mtk_rdma_layer_config(&dev,0,&plane,NULL); assert(load(0x30)==0x40402020 && (load(0x10)&2));
    atomic_context=0; mtk_rdma_clk_disable(&dev); assert(!clock_refs);
}
int main(int argc,char **argv)
{
    assert(argc==9); lifecycle();
    unsigned int width=strtoul(argv[1],NULL,0),height=strtoul(argv[2],NULL,0),bpc=atoi(argv[3]);
    struct clk clk={strtoul(argv[4],NULL,0)};
    unsigned int fps=atoi(argv[5]); bool memory=atoi(argv[6]); u32 seed=strtoul(argv[7],NULL,0);
    bool queued=atoi(argv[8]);
    struct mtk_disp_rdma rdma={.clk=&clk,.regs=regs,.data=match("mediatek,mt6765-disp-rdma")};
    struct device dev={.data=&rdma}; struct cmdq_pkt cmd={0};
    fill(seed); latency=2; trace=true; puts("RESET");
    assert(!mtk_rdma_clk_enable(&dev));
    /* Synthetic dirty setup registers after reset exercise masked updates. */
    fill(seed); writes=0; atomic_context=1; puts("CONFIG");
    mtk_rdma_config(&dev,width,height,fps,bpc,queued?&cmd:NULL);
    assert(rdma.config_valid);
    if (queued) { assert(!writes && cmd.count==21); flush(&cmd); }
    assert((load(0x14)&0x1fff)==width && !(load(0x14)&0xf20000));
    assert((load(0x18)&0xfffff)==height && !(load(0x10)&2));
    assert(!(load(0x24)&0x1f0) && !load(0xf00) && !load(0x2c) && !load(0xa0) && !load(0xa4));
    assert(load(0x40)==0x81800000 && !load(0xb0));
    if (memory) {
        puts("MEMORY"); unsigned int before=writes;
        struct mtk_plane_state plane={.pending={.addr=0x43210000,.pitch=width*4,.format=DRM_FORMAT_XRGB8888}};
        mtk_rdma_layer_config(&dev,0,&plane,queued?&cmd:NULL);
        if (queued) { assert(writes==before && cmd.count); flush(&cmd); }
        assert(load(0xf00)==0x43210000 && load(0x2c)==width*4 && (load(0x10)&2));
        assert(load(0x24)==0x20 && !(load(0x14)&0x20000));
        puts("RETURN"); before=writes;
        mtk_rdma_config(&dev,width,height,fps,0,queued?&cmd:NULL);
        if (queued) { assert(writes==before); flush(&cmd); }
        assert(rdma.config_valid && rdma.bpc==bpc && !(load(0x10)&2));
        assert(!(load(0x24)&0x1f0) && !load(0xf00) && !load(0x2c));
    }
    puts("STOP"); mtk_rdma_start(&dev); assert(load(0x10)&1);
    mtk_rdma_stop(&dev); assert(!(load(0x10)&1));
    atomic_context=0; mtk_rdma_clk_disable(&dev);
}
'''


def harness():
    source=args.source.read_text()
    names=sorted(set(re.findall(r'\bDRM_FORMAT_\w+',source)))
    result=PRELUDE+'enum { '+','.join(names)+' };\n'
    result+=source[source.index('#define DISP_REG_RDMA_INT_ENABLE'):source.index('static irqreturn_t')]
    result+=block(source,'static void rdma_update_bits(')
    for name in ['mtk_rdma_clk_enable','mtk_rdma_clk_disable','mtk_rdma_start','mtk_rdma_stop',
                 'mt6765_rdma_calc_fifo','mt6765_rdma_write_fifo','mt6765_rdma_config','mtk_rdma_config',
                 'rdma_fmt_convert','mtk_rdma_layer_config']:
        match=re.search(r'^(?:static )?(?:int|void|unsigned int) '+name+r'\(',source,re.M)
        assert match,name
        result+=block(source,match[0])
    for name in ['mt2701','mt8173','mt6765','mt8183','mt8195']:
        result+=block(source,'static const struct mtk_disp_rdma_data '+name+'_rdma_driver_data')
    result+=block(source,'static const struct of_device_id mtk_disp_rdma_driver_dt_match')
    return result+MAIN


def check():
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    directory=ROOT/'out/rdma-test';directory.mkdir(exist_ok=True)
    source=directory/'harness.c';binary=directory/'harness';source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
        '-Wno-unused-parameter','-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)],check=True)
    audit=[]
    cases=[(480,640,6,60),(480,640,8,60),(480,640,10,60),(1920,1080,8,60),
           (4096,1,8,60),(8191,16,8,60),(480,640,8,59),(480,640,8,30),(480,640,8,120)]
    for width,height,bpc,fps in cases:
        for high in [0,1]:
            for memory in [0,1]:
                for seed in [0,0xffffffff]:
                    result=subprocess.check_output([str(binary),str(width),str(height),str(bpc),
                        str(457000000 if high else 230000000),str(fps),str(memory),str(seed),str(bool(seed)*1)],text=True)
                    sections={};current=None
                    for line in result.splitlines():
                        if line.startswith('W '): sections[current].append(tuple(int(x,16) for x in line.split()[1:]))
                        else: current=line;sections[current]=[]
                    for mem in range(memory+1):
                        # Stock hardcodes 60 Hz. For other rates compare its
                        # threshold arithmetic at an exactly equivalent pixel
                        # rate; video mode's output-valid threshold is zero.
                        assert width*fps%60==0
                        reference_width=width*fps//60
                        old=stock(raw,reference_width,height,bpc,high,mem,seed)
                        new=[w for w in sections['MEMORY' if mem else 'CONFIG'] if w[0] in GOLDEN_REGS]
                        assert new==old,(width,height,bpc,high,mem,seed,new,old)
                        audit.append({'width':width,'height':height,'bpc':bpc,'fps':fps,
                                      'stock_reference_width':reference_width,'high_clock':high,
                                      'memory':bool(mem),'seed':seed,'writes':new})
                    if memory:
                        assert [w for w in sections['RETURN'] if w[0] in GOLDEN_REGS]==[
                            w for w in sections['CONFIG'] if w[0] in GOLDEN_REGS]
                    old_reset=stock(raw,width,height,bpc,high,memory,seed,True,2)
                    assert sections['RESET'][3:]==old_reset,(sections['RESET'],old_reset)
    print(f'PASS: {len(audit)} native FIFO/QoS traces match stock instructions; direct/memory video input, both stock clocks and dirty states')
    print('PASS: stock reset sequence, bounded timeout cleanup, clock failures, IRQ-safe configuration and legacy MT8183 behavior')
    print('PASS: geometry, input-mode cleanup, invalid modes, CPU/CMDQ write equivalence and memory-to-direct transitions')
    spec=importlib.util.spec_from_file_location('validate',ROOT/'scripts/validate-kernel.py')
    validate=importlib.util.module_from_spec(spec);spec.loader.exec_module(validate)
    node=validate.fdt_nodes(args.dtb.read_bytes())['/soc/rdma@1400d000']
    assert node['compatible']==b'mediatek,mt6765-disp-rdma\0'
    assert 'mediatek,rdma-fifo-size' not in node
    drm=(SRC/'drivers/gpu/drm/mediatek/mtk_drm_drv.c').read_text()
    assert re.search(r'\.compatible = "mediatek,mt6765-disp-rdma",\s*\.data = \(void \*\)MTK_DISP_RDMA',drm)
    print('PASS: native RDMA compatible in compiled DT, platform match data and DRM component lookup')
    (ROOT/'out/mt6765-rdma-audit.json').write_text(json.dumps(audit,indent=2)+'\n')


if __name__=='__main__': check()
