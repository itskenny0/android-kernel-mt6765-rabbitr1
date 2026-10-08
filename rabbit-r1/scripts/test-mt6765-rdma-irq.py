#!/usr/bin/env python3
"""Execute stock RDMA IRQ instructions and exercise production IRQ/clock/probe paths."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4,
    UC_ARM64_REG_X5, UC_ARM64_REG_X6, UC_ARM64_REG_X7, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('rdma', Path(__file__).with_name('test-mt6765-rdma.py'))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)  # Shared --source/--dtb arguments and clock/MMIO model.


def stock_irq(raw, status, late):
    bias,obj,mmio,stop = 0x1000000,0x20000000,0x30000000,0x40000000
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    uc.mem_map(bias,0x2000000); uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000); uc.mem_map(mmio,0x1000); uc.mem_map(stop,0x1000)
    uc.mem_write(mmio+4,struct.pack('<I',status))
    for off in (0xf0,0xf4,0xf8,0xfc): uc.mem_write(mmio+off,struct.pack('<I',off+0x100))
    for reg,value in [(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop),(UC_ARM64_REG_X0,0x103)]:
        uc.reg_write(reg,value)
    counters={'done':0x1a27f60,'start':0x1a27f58,'abnormal':0x1a27f88,
              'underflow':0x1a27f90,'target':0x1a27f70,'underflow_irq':0x1a27f68}
    globals_={**{at:4 for at in counters.values()},0x1a275d0:8,0x1a275e0:8,
              0x1a27f84:4,0x1a27f98:1}
    ack=[]; reads=[]; messages=[]; writeback=None; remaining=status; first_read=True
    registers=[UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3,
               UC_ARM64_REG_X4,UC_ARM64_REG_X5,UC_ARM64_REG_X6,UC_ARM64_REG_X7]
    def code(uc,address,size,_):
        nonlocal writeback
        if writeback is not None:
            uc.mem_write(mmio+4,struct.pack('<I',writeback)); writeback=None
        at=address-bias
        if at in (0x1f01c,0x8bd44,0xa5e2c,0xd9a34,0x69f6bc,0x732564,
                  0x743f94,0x7440ac,0x746e68,0x747728,0x76e230):
            value=0
            if at==0x7440ac: value=0x100+uc.reg_read(UC_ARM64_REG_X0)
            elif at==0x743f94:
                module=uc.reg_read(UC_ARM64_REG_X0); assert module in (3,4),module
                value=mmio+(module-3)*0x1000
            elif at==0x746e68: value=obj
            elif at==0xd9a34: value=0x12345678
            elif at in (0xa5e2c,0x747728):
                # Read format strings from the shipped image, without emulating printk.
                fmt=uc.reg_read(registers[0 if at==0xa5e2c else 1])
                assert bias<=fmt<bias+len(raw),hex(fmt)
                message=bytes(uc.mem_read(fmt,200)).split(b'\0')[0].decode()
                messages.append({'format':message,'arguments':[uc.reg_read(r) for r in registers[1:]]})
            if at!=0x1f01c: uc.reg_write(UC_ARM64_REG_X0,value)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x742260<=at<0x7433c4,hex(at)
    def write(uc,access,address,size,value,_):
        nonlocal writeback,remaining
        if address==mmio+4:
            assert size==4; ack.append(value)
            remaining=(status|late)&value; writeback=remaining
        else:
            assert (obj+0x7000<=address<obj+0x8000 or
                    globals_.get(address-bias)==size),(hex(address-bias),size)
    def read(uc,access,address,size,value,_):
        nonlocal writeback,first_read
        if mmio<=address<mmio+0x1000:
            assert size==4 and address-mmio in (4,0xf0,0xf4,0xf8,0xfc)
            reads.append(address-mmio)
            if address==mmio+4 and first_read:
                # The current LDR gets its old snapshot; the next instruction
                # sees a newly latched event, even when the snapshot was zero.
                writeback=status|late; first_read=False
    uc.hook_add(UC_HOOK_CODE,code); uc.hook_add(UC_HOOK_MEM_WRITE,write); uc.hook_add(UC_HOOK_MEM_READ,read)
    uc.emu_start(bias+0x742260,stop,count=5000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert uc.reg_read(UC_ARM64_REG_X0)==1 and ack==[(~status)&0xffffffff],ack
    values={name:struct.unpack('<I',uc.mem_read(bias+at,4))[0] for name,at in counters.items()}
    assert values=={'done':bool(status&4),'start':bool(status&2),'abnormal':2*bool(status&8),
                    'underflow':bool(status&16),'target':bool(status&32),'underflow_irq':bool(status&16)},values
    # Stock's DDPERR macro evaluates cnt_rdma_abnormal++ twice (logger and printk).
    # Compare event classification, not that incidental double increment.
    assert sorted(set(reads)-{4})==([0xf0,0xf4,0xf8,0xfc] if status&16 else [])
    return {'status':status,'late':late,'ack':ack,'remaining':remaining,'counters':values,
            'reads':reads,'messages':messages}


EXTRA=r'''
#include <stdarg.h>
typedef int irqreturn_t;
#define IRQ_NONE 0
#define IRQ_HANDLED 1
#define IRQF_TRIGGER_NONE 0
#define IRQF_NO_AUTOEN 0x80000
#define GFP_KERNEL 0
#define IS_REACHABLE(x) 0
#define EPROBE_DEFER 517
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((long)(p))
#define dev_dbg(...) ((void)0)
static int failure,pm_refs,components,irq_requested;
static void *allocation,*irq_data;
static const void *match_data;
static struct clk probe_clk={230000000};
static irqreturn_t (*handler)(int,void *);
static unsigned long requested_flags;
static unsigned int callbacks,logs;
static bool log_allowed=true;
static struct device *expected_dev;
struct platform_device { struct device dev; };
struct component_ops { int (*bind)(struct device *,struct device *,void *);
                       void (*unbind)(struct device *,struct device *,void *); };
static void *devm_kzalloc(struct device *dev,size_t size,int flags)
{ (void)dev; (void)flags; return failure==1?NULL:(allocation=calloc(1,size)); }
static int platform_get_irq(struct platform_device *dev,int index)
{ (void)dev; assert(!index); return failure==2?-EPROBE_DEFER:73; }
static struct clk *devm_clk_get(struct device *dev,const char *name)
{ (void)dev; assert(!name); return failure==3?(void *)(intptr_t)-EPROBE_DEFER:&probe_clk; }
static void *devm_platform_ioremap_resource(struct platform_device *dev,int index)
{ (void)dev; assert(!index); return failure==4?(void *)(intptr_t)-ENOMEM:regs; }
static int dev_err_probe(struct device *dev,int error,const char *fmt,...)
{ (void)dev; (void)fmt; return error; }
static int of_property_read_u32(void *node,const char *name,u32 *value)
{
    (void)node; assert(!strcmp(name,"mediatek,rdma-fifo-size"));
    if (failure==5) return -EOVERFLOW;
    if (failure==8) { *value=5120; return 0; }
    return -EINVAL;
}
static const void *of_device_get_match_data(struct device *dev) { (void)dev; return match_data; }
static const char *dev_name(struct device *dev) { (void)dev; return "rdma-test"; }
static int devm_request_irq(struct device *dev,int irq,irqreturn_t (*fn)(int,void *),
                            unsigned long flags,const char *name,void *data)
{
    (void)dev; (void)name; assert(irq==73 && !irq_requested);
    if (failure==6) return -EBUSY;
    requested_flags=flags; handler=fn; irq_data=data; irq_requested=1;
    irq_depth=!!(flags&IRQF_NO_AUTOEN); return 0;
}
static void platform_set_drvdata(struct platform_device *dev,void *data) { dev->dev.data=data; }
static void pm_runtime_enable(struct device *dev) { (void)dev; assert(!pm_refs); pm_refs++; }
static void pm_runtime_disable(struct device *dev) { (void)dev; assert(pm_refs==1); pm_refs--; }
static int component_add(struct device *dev,const struct component_ops *ops)
{
    assert(pm_refs==1 && dev->data==irq_data && ops->bind && ops->unbind);
    if (failure==7) return -EIO;
    components++; return 0;
}
static void component_del(struct device *dev,const struct component_ops *ops)
{ (void)dev; (void)ops; assert(components==1); components--; }
static void capture(struct device *dev,const char *fmt,...)
{
    char message[256]; va_list args; assert(atomic_context && dev==expected_dev);
    va_start(args,fmt); vsnprintf(message,sizeof(message),fmt,args); va_end(args);
    logs++;
    if (strstr(message,"underflow")) assert(strstr(message,"in=496,500 out=504,508"));
}
#undef dev_err_ratelimited
#define dev_err_ratelimited(dev,...) do { if (log_allowed) capture(dev,__VA_ARGS__); } while (0)
static void callback(void *data) { assert(atomic_context && data==&callbacks); callbacks++; }
static void drain_handler(void)
{
    /* Model an already running IRQ completing while disable_irq waits. */
    assert(irq_inflight && clock_refs && !load(0));
    save(4,16); atomic_context=1;
    assert(handler(73,irq_data)==IRQ_HANDLED);
    atomic_context=0;
}
static void __attribute__((unused)) disable_irq_nosync(int irq)
{ (void)irq; assert(clock_refs && !irq_depth); irq_depth++; }
'''

MAIN=r'''
static void initialize(void)
{
    memset(regs,0,sizeof(regs)); save(0x10,0x100); pending=-1; latency=2;
    for (unsigned int off=0xf0;off<=0xfc;off+=4) save(off,off+0x100);
}
static void probe_lifecycle(void)
{
    const int errors[]={0,-ENOMEM,-EPROBE_DEFER,-EPROBE_DEFER,-ENOMEM,-EOVERFLOW,-EBUSY,-EIO,-EINVAL};
    for (failure=0;failure<9;failure++) {
        struct platform_device pdev={0};
        match_data=&mt6765_rdma_driver_data; initialize(); reads=writes=0;
        assert(mtk_disp_rdma_probe(&pdev)==errors[failure]);
        assert(!clock_refs && !reads && !writes);
        if (!failure) {
            assert(pm_refs==1 && components==1 && irq_requested && irq_depth==1);
            assert(requested_flags==IRQF_NO_AUTOEN);
            struct mtk_disp_rdma *rdma=pdev.dev.data;
            assert(rdma->irq==73 && rdma->dev==&pdev.dev); expected_dev=&pdev.dev;
            mtk_rdma_register_vblank_cb(&pdev.dev,callback,&callbacks);
            for (int cycle=0;cycle<5;cycle++) {
                reset_error=cycle==1?1:cycle==2?2:0; initialize();
                assert(mtk_rdma_clk_enable(&pdev.dev)==(reset_error?-ETIMEDOUT:0));
                if (reset_error) { assert(irq_depth==1 && !clock_refs); continue; }
                assert(!irq_depth && clock_refs==1 && !load(0));
                if (cycle==4) {
                    /* CRTC unwinds our successful clock enable if a later component fails. */
                    mtk_rdma_clk_disable(&pdev.dev);
                    assert(irq_depth==1 && !clock_refs); continue;
                }
                mtk_rdma_start(&pdev.dev); assert(!load(0) && !(load(0x10)&1));
                rdma->config_valid=true; mtk_rdma_start(&pdev.dev);
                assert(load(0)==0x18 && (load(0x10)&1));
                save(4,0x1c);
                atomic_context=1; mtk_rdma_enable_vblank(&pdev.dev); atomic_context=0;
                assert(load(0)==0x1c && load(4)==0x18);
                mtk_rdma_start(&pdev.dev); assert(load(0)==0x1c);
                atomic_context=1; mtk_rdma_disable_vblank(&pdev.dev); atomic_context=0;
                assert(load(0)==0x18);
                mtk_rdma_stop(&pdev.dev); assert(!load(0) && !load(4) && !(load(0x10)&1));
                irq_inflight=true; irq_before_disable=drain_handler;
                unsigned int before=rdma->underflow_count;
                mtk_rdma_clk_disable(&pdev.dev);
                assert(rdma->underflow_count==before+1 && !clock_refs && irq_depth==1 && !load(4));
                irq_before_disable=NULL;
            }
            assert(irq_enables==irq_disables && irq_enables==3);
            mtk_rdma_unregister_vblank_cb(&pdev.dev); assert(!rdma->vblank_cb && !rdma->vblank_cb_data);
            mtk_disp_rdma_remove(&pdev);
        }
        assert(!pm_refs && !components && (!irq_requested || irq_depth==1));
        free(allocation); allocation=NULL; irq_requested=0; handler=NULL; irq_data=NULL;
    }
    /* Legacy probe keeps its old interrupt policy and register writes. */
    failure=0; reset_error=0; struct platform_device pdev={0};
    match_data=&mt8183_rdma_driver_data; initialize(); clock_refs=1; reads=writes=0;
    assert(!mtk_disp_rdma_probe(&pdev) && !requested_flags && !irq_depth && writes==2);
    mtk_disp_rdma_remove(&pdev); free(allocation); allocation=NULL; clock_refs=0;
    irq_requested=0; irq_depth=1;
}
int main(int argc,char **argv)
{
    assert(argc==5); probe_lifecycle();
    struct mtk_disp_rdma rdma={.regs=regs,.data=&mt6765_rdma_driver_data};
    struct device dev={.data=&rdma}; rdma.dev=&dev; expected_dev=&dev;
    u32 status=strtoul(argv[1],NULL,0),enabled=strtoul(argv[2],NULL,0),late=strtoul(argv[4],NULL,0);
    bool registered=atoi(argv[3]);
    initialize(); clock_refs=1; atomic_context=1; reads=writes=logs=callbacks=0;
    if (registered) mtk_rdma_register_vblank_cb(&dev,callback,&callbacks);
    save(0,enabled); save(4,status); late_status=late;
    irqreturn_t ret=mtk_disp_rdma_irq_handler(73,&rdma);
    assert(ret==(status?IRQ_HANDLED:IRQ_NONE));
    assert(writes==!!status && load(4)==(late&~status));
    assert(callbacks==!!(registered && (status&enabled&4)));
    assert(rdma.abnormal_count==!!(status&8) && rdma.underflow_count==!!(status&16));
    assert(logs==(unsigned int)(!!(status&8)+!!(status&16)));
    printf("%u %u %u %u %u\n",callbacks,rdma.abnormal_count,rdma.underflow_count,load(4),writes);
    /* Rate limiting must not suppress event accounting, nor evaluate MMIO arguments. */
    log_allowed=false; save(4,0x18); unsigned int before=reads;
    assert(mtk_disp_rdma_irq_handler(73,&rdma)==IRQ_HANDLED && reads==before+2);
    assert(rdma.abnormal_count==!!(status&8)+1 && rdma.underflow_count==!!(status&16)+1);
    /* IRQ behavior on the older platforms remains unchanged. */
    rdma.data=&mt8183_rdma_driver_data; rdma.vblank_cb=NULL; save(4,0x18);
    assert(mtk_disp_rdma_irq_handler(73,&rdma)==IRQ_NONE && !load(4));
    mtk_rdma_register_vblank_cb(&dev,callback,&callbacks); before=callbacks;
    assert(mtk_disp_rdma_irq_handler(73,&rdma)==IRQ_HANDLED && callbacks==before+1);
    /* Keep the shared CMDQ model compiled with strict unused-function checks. */
    struct cmdq_pkt pkt={0}; mtk_ddp_write_relaxed(&pkt,0,NULL,regs,0);
    mtk_ddp_write_mask(&pkt,0,NULL,regs,0,~0U); flush(&pkt);
}
'''


def harness():
    source=shared.args.source.read_text()
    import re
    result=shared.PRELUDE+EXTRA+'enum { '+','.join(sorted(set(re.findall(r'\bDRM_FORMAT_\w+',source))))+' };\n'
    result+=source[source.index('#define DISP_REG_RDMA_INT_ENABLE'):source.index('static irqreturn_t')]
    for name in ['static irqreturn_t mt6765_rdma_irq(', 'static irqreturn_t mtk_disp_rdma_irq_handler(',
                 'static void rdma_update_bits(', 'void mtk_rdma_register_vblank_cb(',
                 'void mtk_rdma_unregister_vblank_cb(', 'void mtk_rdma_enable_vblank(',
                 'void mtk_rdma_disable_vblank(', 'int mtk_rdma_clk_enable(', 'void mtk_rdma_clk_disable(',
                 'void mtk_rdma_start(', 'void mtk_rdma_stop(', 'static int mtk_disp_rdma_bind(',
                 'static void mtk_disp_rdma_unbind(', 'static const struct component_ops mtk_disp_rdma_component_ops',
                 'static int mtk_disp_rdma_probe(', 'static void mtk_disp_rdma_remove(',
                 'static const struct mtk_disp_rdma_data mt6765_rdma_driver_data',
                 'static const struct mtk_disp_rdma_data mt8183_rdma_driver_data']:
        result+=shared.block(source,name)
    return result+MAIN


def check():
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    directory=ROOT/'out/rdma-irq-test'; directory.mkdir(exist_ok=True)
    source=directory/'harness.c';binary=directory/'harness';source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
        '-Wno-unused-parameter','-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)],check=True)
    audit=[]; count=0
    for status in list(range(128))+[0x80000000,0xffffffff]:
        for late in [0,0x1c]:
            stock=stock_irq(raw,status,late); audit.append(stock)
            for enabled in [0,0x18,0x1c]:
                for registered in [0,1]:
                    result=subprocess.check_output([str(binary),str(status),str(enabled),str(registered),str(late)],text=True)
                    callback,abnormal,underflow,remaining,writes=map(int,result.split())
                    assert callback==bool(registered and enabled&4 and stock['counters']['done'])
                    assert bool(abnormal)==bool(stock['counters']['abnormal'])
                    assert underflow==stock['counters']['underflow']
                    assert remaining==stock['remaining']
                    count+=1
    (ROOT/'out/mt6765-rdma-irq-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(f'PASS: {len(audit)} stock IRQ traces and {count} native IRQ cases; event classification, selective ACK, late events, vblank gating and diagnostics')
    print('PASS: probe without MMIO, eight probe failures, reset failures, repeated clock/IRQ balance, synchronized shutdown and MT8183 regression')
    print('No hardware IRQ, GIC or power domain is emulated; clocks, MMIO side effects and kernel services are models.')


if __name__=='__main__': check()
