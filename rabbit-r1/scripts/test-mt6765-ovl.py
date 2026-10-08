#!/usr/bin/env python3
"""Compare native overlay setup with shipped instructions and test IRQ/clock lifetime."""
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
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE,UC_HOOK_MEM_WRITE,UC_HOOK_MEM_READ
from unicorn.arm64_const import (UC_ARM64_REG_PC,UC_ARM64_REG_SP,UC_ARM64_REG_X0,
    UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3,UC_ARM64_REG_X4,UC_ARM64_REG_X30,
    UC_ARM64_REG_X20,UC_ARM64_REG_X22,UC_ARM64_REG_X23,UC_ARM64_REG_X24,
    UC_ARM64_REG_X25,UC_ARM64_REG_X26)

ROOT=Path('/rabbitr1'); SRC=ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
os.environ['TMPDIR']=str(ROOT/'.tmp')
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source',type=Path,default=SRC/'drivers/gpu/drm/mediatek/mtk_disp_ovl.c')
parser.add_argument('--dtb',type=Path,default=ROOT/'out/mainline/arch/arm64/boot/dts/mediatek/mt6765-rabbit-r1.dtb')
args=parser.parse_args()
for path in vars(args).values():
    if not path.resolve().is_relative_to(ROOT): raise SystemExit('Inputs must be under /rabbitr1')
RANGES={'golden':(0x6fffe4,0x700b14),'start':(0x6f71a4,0x6f7600),'stop':(0x6f7600,0x6f7814),
        'reset':(0x6f7854,0x6f7908),'roi':(0x6f7908,0x6f7ca0),'layers':(0x6f7ca0,0x6f80ac)}


def stock(raw,kind,module,seed,width=480,height=640,status=0,late=0):
    bias,obj,mmio,stop=0x1000000,0x20000000,0x30000000,0x40000000
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000);uc.mem_map(mmio,0x1000);uc.mem_map(stop,0x1000)
    uc.mem_write(mmio,struct.pack('<I',seed)*1024)
    uc.mem_write(mmio+0x240,struct.pack('<I',1));uc.mem_write(obj,bytes(16))
    if kind=='irq':uc.mem_write(mmio+8,struct.pack('<I',status))
    params=([module,0,12,obj,0] if kind=='golden' else [module,width,height,0xff000000,0]
            if kind=='roi' else [0x100+module,0,0,0,0] if kind=='irq' else [module,0,0,0,0])
    for reg,value in zip([UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3,UC_ARM64_REG_X4],params):
        uc.reg_write(reg,value)
    uc.reg_write(UC_ARM64_REG_SP,obj+0x8000);uc.reg_write(UC_ARM64_REG_X30,stop)
    events=[];messages=[];writeback=None;first_read=True
    def code(uc,address,size,_):
        nonlocal writeback
        if writeback is not None:
            uc.mem_write(mmio+8,struct.pack('<I',writeback));writeback=None
        at=address-bias
        if at in (0x1f01c,0x743f94,0x76db80,0x76dc34,0x747728,0xa5e2c,0x74398c,
                  0x7440ac,0x744368,0x732564,0x746e68,0x69f6bc,0x8bd44):
            value=0
            if at==0x743f94:
                assert uc.reg_read(UC_ARM64_REG_X0)==module;value=mmio
            elif at==0x76db80:value=width
            elif at==0x76dc34:value=height
            elif at==0x7440ac:value=0x100+uc.reg_read(UC_ARM64_REG_X0)
            elif at==0x744368:
                assert uc.reg_read(UC_ARM64_REG_X0)==0x100+module;value=module
            elif at==0x732564:value=1  # Enable stock debug event classification.
            elif at==0x746e68:value=obj
            elif at in (0x747728,0xa5e2c):
                fmt=uc.reg_read(UC_ARM64_REG_X1 if at==0x747728 else UC_ARM64_REG_X0)
                assert bias<=fmt<bias+len(raw),hex(fmt)
                messages.append(bytes(uc.mem_read(fmt,200)).split(b'\0')[0].decode())
            if at!=0x1f01c:uc.reg_write(UC_ARM64_REG_X0,value)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            begin,end=(0x742260,0x7433c4) if kind=='irq' else RANGES[kind]
            assert begin<=at<end or 0x6f7010<=at<0x6f7088 or 0x6f7110<=at<0x6f71a4,hex(at)
    def write(uc,access,address,size,value,_):
        nonlocal writeback
        if mmio<=address<mmio+0x1000:
            assert size==4;events.append((address-mmio,value))
            if kind=='irq':
                assert address==mmio+8
                writeback=(status|late)&value
        else:
            assert obj+0x7000<=address<obj+0x8000 or (kind=='irq' and
                address==bias+0x1a27f78+4*module and size==4),(hex(address-bias),size)
    def read(uc,access,address,size,value,_):
        nonlocal writeback,first_read
        if kind=='irq' and address==mmio+8 and first_read:
            writeback=status|late;first_read=False
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write);uc.hook_add(UC_HOOK_MEM_READ,read)
    uc.emu_start(bias+(0x742260 if kind=='irq' else RANGES[kind][0]),stop,count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert uc.reg_read(UC_ARM64_REG_X0)==(1 if kind=='irq' else 0)
    if kind=='irq':
        assert events==[(8,(~status)&0xffffffff)]
        assert struct.unpack('<I',uc.mem_read(mmio+8,4))[0]==late&~status
        assert any('frame done' in message for message in messages)==bool(status&2)
        assert any('frame underflow' in message for message in messages)==bool(status&4)
        assert any('abnormal SOF' in message for message in messages)==bool(status&0x2000)
        for layer in range(4):
            assert any(f'-L{layer} not complete' in message for message in messages)==bool(status&(32<<layer))
    return {'kind':kind,'module':module,'seed':seed,'width':width,'height':height,'status':status,
            'late':late,'writes':events,'messages':messages}


def block(source,start):
    begin=source.index(start);end=source.index('\n}',begin)+2
    if source[end:end+1]==';':end+=1
    return source[begin:end]+'\n'


def stock_sbch(raw,module,seed,feature,config,queued):
    """Execute ovl_config_l's post-layer branch through SBCH cleanup only."""
    assert not (feature and config), 'Enabled SBCH tracking is outside this fixture'
    bias,obj,mmio,phys=0x1000000,0x20000000,0x30000000,0x1400b000
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000);uc.mem_map(mmio,0x2000)
    base=mmio+module*0x1000
    uc.mem_write(mmio,struct.pack('<I',seed)*2048)
    uc.mem_write(obj+0xf04,struct.pack('<I',config))
    handle=obj+0x2000 if queued else 0
    uc.mem_write(obj+0x8098,struct.pack('<Q',handle))
    registers={UC_ARM64_REG_SP:obj+0x8000,UC_ARM64_REG_X20:module,UC_ARM64_REG_X22:obj,
               UC_ARM64_REG_X24:handle,UC_ARM64_REG_X26:1,UC_ARM64_REG_X25:3,UC_ARM64_REG_X23:0x10000}
    for reg,value in registers.items():uc.reg_write(reg,value)
    events=[];packets=[];options=[];clears=[]
    cache=bias+0x19f8278
    uc.mem_write(cache,b'\xa5'*0x870)
    def code(uc,address,size,_):
        at=address-bias
        a,b,c,d=[uc.reg_read(r) for r in [UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3]]
        if at in (0x1f01c,0x743f94,0x743cb0,0x7a2740,0x747728,0xf1e480,0x6c0ca4):
            result=0
            if at==0x1f01c:result=a
            elif at in (0x743f94,0x743cb0):
                assert a in (0,1)
                result=(mmio if at==0x743f94 else phys)+a*0x1000
            elif at==0x7a2740:
                assert a==0x37;options.append(a);result=feature
            elif at==0xf1e480:
                assert (a,b,c)==(cache,0,0x870)
                uc.mem_write(a,bytes(c));clears.append(c);result=a
            elif at==0x6c0ca4:
                assert queued and a==handle
                offset=b-phys-module*0x1000
                assert offset in (0x2c,0x324,0x3a0,0x3a4)
                assert d==(0xf if offset==0x2c else 0xffffffff)
                packets.append((offset,c,d))
            uc.reg_write(UC_ARM64_REG_X0,result)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            allowed=((0x6fed38,0x6feeec),(0x6ff970,0x6ffa90),(0x6ffab0,0x6ffad4),
                     (0x6ffbe8,0x6ffd28),(0x6f7010,0x6f7088),(0x6f7110,0x6f71a4))
            assert any(lo<=at<hi for lo,hi in allowed),hex(at)
    def write(uc,access,address,size,value,_):
        if base<=address<base+0x1000:
            assert not queued and size==4
            events.append((address-base,value))
        else:
            assert obj+0x7000<=address and address+size<=obj+0x8000,hex(address)
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+0x6fed38,bias+0x6ffd28,count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC)==bias+0x6ffd28
    assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert options==[0x37] and clears==([0x870] if feature else [])
    assert bytes(uc.mem_read(cache,0x870))==(bytes(0x870) if feature else b'\xa5'*0x870)
    if queued:
        assert not events
        for offset,value,mask in packets:
            before=struct.unpack('<I',uc.mem_read(base+offset,4))[0]
            after=(before&~mask)|(value&mask)
            uc.mem_write(base+offset,struct.pack('<I',after));events.append((offset,after))
    assert events==[(0x2c,(seed&~0xf)|1),(0x324,0x10003),(0x3a0,0),(0x3a4,0)],events
    assert struct.unpack('<I',uc.mem_read(base+0x3a8,4))[0]==seed
    return {'module':module,'seed':seed,'feature':feature,'config':config,'queued':queued,
            'writes':events,'packets':packets,'cache_cleared':bool(clears)}


PRELUDE=r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef int irqreturn_t;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define IRQ_NONE 0
#define IRQ_HANDLED 1
#define IRQF_TRIGGER_NONE 0
#define IRQF_NO_AUTOEN 0x80000
#define GFP_KERNEL 0
#define EPROBE_DEFER 517
#define IS_REACHABLE(x) 0
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((long)(p))
#define dev_err(...) ((void)0)
#define dev_dbg(...) ((void)0)
static bool log_allowed=true;
static unsigned int logs;
static unsigned int diagnostic;
#define dev_err_ratelimited(dev,fmt,...) do { if (log_allowed) { \
    char line[256]; assert(dev); snprintf(line,sizeof(line),fmt,__VA_ARGS__); \
    if (strstr(line,"OVL error:")) { diagnostic++; } logs++; } } while (0)
struct clk { int unused; };
struct device { void *data; };
struct platform_device { struct device dev; };
struct cmdq_client_reg { int unused; };
struct of_device_id { const char *compatible; const void *data; };
struct component_ops { int (*bind)(struct device *,struct device *,void *);
                       void (*unbind)(struct device *,struct device *,void *); };
static void *dev_get_drvdata(struct device *dev) { return dev->data; }
static u32 regs[1024],late_status;
static unsigned int reads,writes,clock_refs,polls,irq_depth=1,enables,disables;
static unsigned int sbch_writes;
static unsigned int callbacks;
static bool trace,atomic_context,irq_inflight;
static int clock_error,reset_error,latency,remaining=-1,failure,pm_refs,components,requested;
static void *allocation,*irq_data;
static const void *match_data;
static struct clk clock_;
static irqreturn_t (*handler)(int,void *);
static void (*drain)(void);
static unsigned long requested_flags;
static u32 load(unsigned int off) { assert(off%4==0 && off<sizeof(regs)); return regs[off/4]; }
static void save(unsigned int off,u32 val) { assert(off%4==0 && off<sizeof(regs)); regs[off/4]=val; }
static u32 readl(const void *ptr)
{
    assert(clock_refs && (uintptr_t)ptr>=(uintptr_t)regs && (uintptr_t)ptr<(uintptr_t)regs+sizeof(regs));
    unsigned int off=(const u8 *)ptr-(const u8 *)regs; reads++;
    if (off==0x240 && remaining>=0) {
        if (remaining) remaining--;
        else if (!reset_error) { save(off,(load(off)&~3U)|1); remaining=-1; }
    }
    u32 value=load(off);
    if (off==8 && late_status) { save(off,value|late_status); late_status=0; }
    return value;
}
static void writel(u32 val,void *ptr)
{
    assert(clock_refs && (uintptr_t)ptr>=(uintptr_t)regs && (uintptr_t)ptr<(uintptr_t)regs+sizeof(regs));
    unsigned int off=(u8 *)ptr-(u8 *)regs;
    if (off==0x3a0 || off==0x3a4) {
        assert(!val && off==0x3a0+4*sbch_writes && sbch_writes<2);
        assert(irq_depth==1 && !load(4) && !(load(0xc)&1) && !load(0x14) && (load(0x240)&3));
        assert(!load(0x2c) && !load(0x324)); sbch_writes++;
    }
    if (off==0x14) {
        if (val==1) { save(0x240,load(0x240)&~3U); remaining=-1; }
        else { assert(val==0); remaining=latency; }
    }
    save(off,off==8?load(off)&val:val); writes++;
    if (trace) printf("W %x %x\n",off,val);
}
#define writel_relaxed writel
#define readl_poll_timeout(addr,val,cond,delay,timeout) ({ \
    assert(!atomic_context && (delay)==10 && (timeout)==20000); \
    int result=-ETIMEDOUT; \
    for (int i=0;i<8;i++) { polls++; val=readl(addr); if (cond) { result=0; break; } } result; })
static int clk_prepare_enable(struct clk *clk)
{ (void)clk; assert(!atomic_context); if (clock_error) return clock_error; clock_refs++; return 0; }
static void clk_disable_unprepare(struct clk *clk)
{ (void)clk; assert(!atomic_context && clock_refs && !irq_inflight); clock_refs--; }
static void enable_irq(int irq)
{
    assert(irq==73 && !atomic_context && clock_refs && irq_depth==1 && !load(4));
    assert(!load(0x14) && (load(0x240)&3) && !(load(0xc)&1)); irq_depth--; enables++;
    assert(sbch_writes==2 && !load(0x3a0) && !load(0x3a4));
}
static void disable_irq(int irq)
{
    assert(irq==73 && !atomic_context && clock_refs && !irq_depth && !load(4));
    irq_depth++; disables++; if (drain) drain(); irq_inflight=false;
}
static void __attribute__((unused)) disable_irq_nosync(int irq)
{ (void)irq; assert(clock_refs && !irq_depth); irq_depth++; }
struct queued { u32 offset,value,mask; };
struct cmdq_pkt { struct queued ops[128]; size_t count; };
static void queue(struct cmdq_pkt *pkt,u32 val,void *base,u32 off,u32 mask)
{
    assert(base==regs && off<sizeof(regs));
    if (pkt) { assert(pkt->count<128); pkt->ops[pkt->count++]=(struct queued){off,val,mask}; }
    else writel((readl((u8 *)base+off)&~mask)|(val&mask),(u8 *)base+off);
}
static void mtk_ddp_write(struct cmdq_pkt *pkt,u32 val,struct cmdq_client_reg *reg,void *base,u32 off)
{ (void)reg; queue(pkt,val,base,off,~0U); }
#define mtk_ddp_write_relaxed mtk_ddp_write
static void mtk_ddp_write_mask(struct cmdq_pkt *pkt,u32 val,struct cmdq_client_reg *reg,void *base,u32 off,u32 mask)
{ (void)reg; queue(pkt,val,base,off,mask); }
static void flush(struct cmdq_pkt *pkt)
{ for (size_t i=0;i<pkt->count;i++) queue(NULL,pkt->ops[i].value,regs,pkt->ops[i].offset,pkt->ops[i].mask); pkt->count=0; }
static void *devm_kzalloc(struct device *dev,size_t size,int flags)
{ (void)dev; (void)flags; return failure==1?NULL:(allocation=calloc(1,size)); }
static int platform_get_irq(struct platform_device *dev,int index)
{ (void)dev; assert(!index); return failure==2?-EPROBE_DEFER:73; }
static struct clk *devm_clk_get(struct device *dev,const char *name)
{ (void)dev; assert(!name); return failure==3?(void *)(intptr_t)-EPROBE_DEFER:&clock_; }
static void *devm_platform_ioremap_resource(struct platform_device *dev,int index)
{ (void)dev; assert(!index); return failure==4?(void *)(intptr_t)-ENOMEM:regs; }
static int dev_err_probe(struct device *dev,int error,const char *fmt,...)
{ (void)dev; (void)fmt; return error; }
static const void *of_device_get_match_data(struct device *dev) { (void)dev; return match_data; }
static const char *dev_name(struct device *dev) { (void)dev; return "ovl-test"; }
static int devm_request_irq(struct device *dev,int irq,irqreturn_t (*fn)(int,void *),
                            unsigned long flags,const char *name,void *data)
{
    (void)dev; (void)name; assert(irq==73 && !requested); if (failure==5) return -EBUSY;
    requested_flags=flags; handler=fn; irq_data=data; requested=1; irq_depth=!!(flags&IRQF_NO_AUTOEN); return 0;
}
static void platform_set_drvdata(struct platform_device *dev,void *data) { dev->dev.data=data; }
static void pm_runtime_enable(struct device *dev) { (void)dev; assert(!pm_refs); pm_refs++; }
static void pm_runtime_disable(struct device *dev) { (void)dev; assert(pm_refs==1); pm_refs--; }
static int component_add(struct device *dev,const struct component_ops *ops)
{ assert(pm_refs==1 && dev->data==irq_data && ops->bind && ops->unbind); if (failure==6) return -EIO; components++; return 0; }
static void component_del(struct device *dev,const struct component_ops *ops)
{ (void)dev; (void)ops; assert(components==1); components--; }
static void callback(void *data) { assert(atomic_context && data==&callbacks); callbacks++; }
static void finish_irq(void)
{
    assert(irq_inflight && clock_refs && !load(4));
    save(8,4); atomic_context=true; assert(handler(73,irq_data)==IRQ_HANDLED); atomic_context=false;
}
'''

MAIN=r'''
static const struct mtk_disp_ovl_data *match(const char *name)
{
    for (const struct of_device_id *p=mtk_disp_ovl_driver_dt_match;p->compatible;p++)
        if (!strcmp(name,p->compatible)) return p->data;
    assert(false); return NULL;
}
static void fill(u32 seed)
{ for (unsigned int off=0;off<sizeof(regs);off+=4) save(off,seed); remaining=-1; sbch_writes=0; }
static void lifecycle(const struct mtk_disp_ovl_data *data)
{
    const int errors[]={0,-ENOMEM,-EPROBE_DEFER,-EPROBE_DEFER,-ENOMEM,-EBUSY,-EIO};
    for (failure=0;failure<7;failure++) {
        struct platform_device pdev={0}; match_data=data; reads=writes=0;
        assert(mtk_disp_ovl_probe(&pdev)==errors[failure]);
        assert(!clock_refs && !reads && !writes);
        if (!failure) {
            assert(requested_flags==IRQF_NO_AUTOEN && irq_depth==1 && pm_refs==1 && components==1);
            struct mtk_disp_ovl *ovl=pdev.dev.data;
            assert(ovl->dev==&pdev.dev && ovl->irq==73);
            unsigned int before_enables=enables;
            for (unsigned int cycle=0;cycle<5;cycle++) {
                fill(~0U); polls=0; latency=2; reset_error=cycle==1; clock_error=cycle==2?-EIO:0;
                int ret=mtk_ovl_clk_enable(&pdev.dev);
                assert(ret==(clock_error?clock_error:reset_error?-ETIMEDOUT:0));
                if (ret) {
                    assert(!ovl->clock_enabled && !ovl->config_valid && !clock_refs && irq_depth==1);
                    assert(!sbch_writes && load(0x3a0)==~0U && load(0x3a4)==~0U); continue;
                }
                assert(clock_refs==1 && !irq_depth && ovl->clock_enabled && polls==3);
                assert(!load(0x2c) && !load(0x324) && !load(8));
                assert(sbch_writes==2 && !load(0x3a0) && !load(0x3a4) && load(0x3a8)==~0U);
                assert(!(load(0x24)&6));
                for (unsigned int i=0;i<data->layer_nr;i++) assert(!load(0xc0+0x20*i));
                unsigned int before=writes; mtk_ovl_start(&pdev.dev); assert(writes==before);
                if (cycle!=4) {
                    atomic_context=true; mtk_ovl_config(&pdev.dev,480,640,59,8,NULL); mtk_ovl_start(&pdev.dev);
                    u32 errors=4|0x2000|(((1U<<data->layer_nr)-1)<<5);
                    assert(load(4)==errors && (load(0xc)&0x401)==0x401);
                    save(8,errors|2); mtk_ovl_enable_vblank(&pdev.dev);
                    assert(load(8)==errors && load(4)==(errors|2));
                    mtk_ovl_start(&pdev.dev); assert(load(4)==(errors|2));
                    mtk_ovl_disable_vblank(&pdev.dev); assert(load(4)==errors);
                    mtk_ovl_stop(&pdev.dev); assert(!ovl->config_valid && !load(4) && !load(8) && !(load(0xc)&1));
                    atomic_context=false;
                }
                before=ovl->error_count;
                irq_inflight=true; drain=finish_irq; mtk_ovl_clk_disable(&pdev.dev); drain=NULL;
                assert(ovl->error_count==before+1 && !clock_refs && irq_depth==1 && !ovl->clock_enabled);
            }
            assert(enables-before_enables==3 && enables==disables);
            mtk_disp_ovl_remove(&pdev);
        }
        assert(!pm_refs && !components && (!requested || irq_depth==1));
        free(allocation);allocation=NULL;requested=0;handler=NULL;irq_data=NULL;
    }
    failure=clock_error=reset_error=0;
    struct platform_device old={0};match_data=&mt8192_ovl_driver_data;reads=writes=0;
    assert(!mtk_disp_ovl_probe(&old) && !reads && !writes && !requested_flags && !irq_depth);
    mtk_disp_ovl_remove(&old);free(allocation);allocation=NULL;requested=0;irq_depth=1;
}
static void irq_cases(struct device *dev)
{
    struct mtk_disp_ovl *ovl=dev->data;
    u32 error_mask=4|0x2000|(((1U<<ovl->data->layer_nr)-1)<<5);
    atomic_context=true; trace=false;
    for (u32 status=0;status<0x8000;status++) {
        for (unsigned int cb=0;cb<2;cb++) {
            callbacks=logs=diagnostic=0; ovl->error_count=0; writes=0;
            if (cb) mtk_ovl_register_vblank_cb(dev,callback,&callbacks);
            else mtk_ovl_unregister_vblank_cb(dev);
            u32 enabled=status&1?error_mask:0x7fff;
            save(4,enabled);save(8,status);late_status=0x206;
            assert(mtk_disp_ovl_irq_handler(73,ovl)==(status?IRQ_HANDLED:IRQ_NONE));
            assert(writes==!!status && load(8)==(0x206&~status));
            assert(callbacks==!!(cb && (status&enabled&2)));
            assert(ovl->error_count==!!(status&error_mask) && diagnostic==ovl->error_count);
        }
    }
    log_allowed=false;save(8,4);reads=0;unsigned int before=ovl->error_count;
    assert(mtk_disp_ovl_irq_handler(73,ovl)==IRQ_HANDLED && reads==2 && ovl->error_count==before+1);
    log_allowed=true; mtk_ovl_unregister_vblank_cb(dev); atomic_context=false;
}
int main(int argc,char **argv)
{
    assert(argc==6);unsigned int module=atoi(argv[1]);u32 seed=strtoul(argv[2],NULL,0);
    bool queued=atoi(argv[3]);unsigned int width=atoi(argv[4]),height=atoi(argv[5]);
    const struct mtk_disp_ovl_data *data=match(module?"mediatek,mt6765-disp-ovl-2l":"mediatek,mt6765-disp-ovl");
    assert(data->mt6765 && data->layer_nr==(module?2:4) && !data->supports_afbc && !data->supports_clrfmt_ext);
    assert(data->addr==0xf40 && data->num_formats==11);
    lifecycle(data);
    struct mtk_disp_ovl ovl={.clk=&clock_,.regs=regs,.data=data,.irq=73};
    struct device dev={&ovl};ovl.dev=&dev;
    fill(seed);latency=2;trace=true;puts("RESET");assert(!mtk_ovl_clk_enable(&dev));
    /* Dirty registers after modeled reset test complete ownership of setup fields. */
    fill(seed);save(4,0);save(0x14,0);save(0xc,load(0xc)&~1U);writes=0;
    save(0x3a0,0);save(0x3a4,0); /* Cleanup belongs to clock enable, not per-frame config. */
    struct cmdq_pkt pkt={0};atomic_context=true;puts("CONFIG");
    mtk_ovl_config(&dev,width,height,59,8,queued?&pkt:NULL);
    assert(ovl.config_valid);if (queued) { assert(!writes && pkt.count);flush(&pkt); }
    assert(load(0x20)==(height<<16|width) && load(0x28)==0xff000000 && !load(0x14));
    trace=false;
    if (module) mtk_ovl_bgclr_in_on(&dev); else mtk_ovl_bgclr_in_off(&dev);
    trace=true;
    puts("START");mtk_ovl_start(&dev);assert((load(0xc)&0x401)==0x401 && !(load(0xc)&0xc0000));
    assert((load(0x24)&0x5000009)==0x5000001);
    puts("LAYERS");
    for (unsigned int i=0;i<data->layer_nr;i++) {
        unsigned int before=writes;mtk_ovl_layer_on(&dev,i,queued?&pkt:NULL);
        if (queued) { assert(writes==before);flush(&pkt); }
        assert(load(0xc8+0x20*i)==0x03ff03ff && load(0xc0+0x20*i)==1 && (load(0x2c)&BIT(i)));
        mtk_ovl_layer_off(&dev,i,queued?&pkt:NULL);if (queued) flush(&pkt);
        assert(!load(0xc0+0x20*i) && !(load(0x2c)&BIT(i)));
    }
    puts("STOP");mtk_ovl_stop(&dev);atomic_context=false;trace=false;
    irq_cases(&dev);
    const unsigned int bad[][2]={{0,640},{480,0},{4096,640},{480,4096},{65536,1}};
    for (size_t i=0;i<ARRAY_SIZE(bad);i++) {
        atomic_context=true;mtk_ovl_config(&dev,480,640,59,8,NULL);mtk_ovl_start(&dev);
        mtk_ovl_config(&dev,bad[i][0],bad[i][1],59,8,NULL);
        assert(!ovl.config_valid && !(load(0xc)&1));unsigned int before=writes;
        mtk_ovl_start(&dev);assert(writes==before);atomic_context=false;
    }
    mtk_ovl_clk_disable(&dev);assert(!clock_refs && irq_depth==1);
    unsigned int before=writes;atomic_context=true;mtk_ovl_config(&dev,480,640,59,8,NULL);
    mtk_ovl_start(&dev);atomic_context=false;assert(writes==before);
    /* The old MT8192 path keeps its reset, GMC, startup and IRQ behavior. */
    ovl.data=match("mediatek,mt8192-disp-ovl");fill(0);writes=0;
    save(0x3a0,~0U);save(0x3a4,~0U);save(0x3a8,~0U);
    assert(!mtk_ovl_clk_enable(&dev) && !writes);
    atomic_context=true;mtk_ovl_config(&dev,480,640,59,8,NULL);assert(writes==4);
    mtk_ovl_start(&dev);assert(load(0xc)==1 && load(0x24)==1);
    mtk_ovl_layer_on(&dev,0,NULL);assert(load(0xc8)==0x1000100);
    mtk_ovl_enable_vblank(&dev);assert(load(4)==2);mtk_ovl_disable_vblank(&dev);assert(!load(4));
    mtk_ovl_register_vblank_cb(&dev,callback,&callbacks);before=callbacks;
    assert(mtk_disp_ovl_irq_handler(73,&ovl)==IRQ_HANDLED && callbacks==before+1);
    mtk_ovl_stop(&dev);assert(!load(0xc) && !load(0x24));atomic_context=false;mtk_ovl_clk_disable(&dev);
    assert(!clock_refs && enables==disables);
    assert(!sbch_writes && load(0x3a0)==~0U && load(0x3a4)==~0U && load(0x3a8)==~0U);
}
'''


def harness():
    source=args.source.read_text()
    enums=sorted(set(re.findall(r'\bDRM_(?:FORMAT_\w+|MODE_BLEND_\w+)',source)))
    result=PRELUDE+'enum { '+','.join(enums)+' };\n'
    result+=source[source.index('#define DISP_REG_OVL_INTEN'):source.index('static inline bool is_10bit_rgb')]
    for name in ['static const u32 mt8173_formats','static const u32 mt8195_formats',
                 'struct mtk_disp_ovl_data {','struct mtk_disp_ovl {',
                 'static u32 mt6765_ovl_error_mask(', 'static irqreturn_t mt6765_ovl_irq(',
                 'static irqreturn_t mtk_disp_ovl_irq_handler(']:result+=block(source,name)
    names=['mtk_ovl_register_vblank_cb','mtk_ovl_unregister_vblank_cb','mtk_ovl_enable_vblank','mtk_ovl_disable_vblank',
           'mtk_ovl_clk_enable','mtk_ovl_clk_disable','mtk_ovl_start','mtk_ovl_stop','mt6765_ovl_config','mtk_ovl_config',
           'mtk_ovl_layer_on','mtk_ovl_layer_off','mtk_ovl_bgclr_in_on','mtk_ovl_bgclr_in_off',
           'mtk_disp_ovl_bind','mtk_disp_ovl_unbind']
    for name in names:
        match=re.search(r'^(?:static )?(?:int|void) '+name+r'\(',source,re.M);assert match,name
        result+=block(source,match[0])
    result+=block(source,'static const struct component_ops mtk_disp_ovl_component_ops')
    result+=block(source,'static int mtk_disp_ovl_probe(')+block(source,'static void mtk_disp_ovl_remove(')
    for name in ['mt2701','mt8167','mt8173','mt8183','mt8183_ovl_2l','mt8192','mt8192_ovl_2l','mt8195','mt6765','mt6765_ovl_2l']:
        key=name if '_ovl_' in name else name+'_ovl'
        result+=block(source,'static const struct mtk_disp_ovl_data '+key+'_driver_data')
    return result+block(source,'static const struct of_device_id mtk_disp_ovl_driver_dt_match')+MAIN


def check():
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    folder=ROOT/'out/ovl-test';folder.mkdir(exist_ok=True)
    source=folder/'harness.c';binary=folder/'harness';source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)],check=True)
    audit=[];sbch_audit=[];fixtures=0
    for module in [0,1]:
        for seed in [0,0xffffffff,0xa5a55a5a]:
            cleanup=[stock_sbch(raw,module,seed,feature,config,queued)
                     for feature,config in [(0,0),(0,1),(1,0)] for queued in [False,True]]
            sbch_audit.extend(cleanup)
            for width,height in [(480,640),(4095,4095),(1,1)]:
                result=subprocess.check_output([str(binary),str(module),str(seed),str(seed&1),str(width),str(height)],text=True)
                sections={};current=None
                for line in result.splitlines():
                    if line.startswith('W '):sections[current].append(tuple(int(x,16) for x in line.split()[1:]))
                    else:current=line;sections[current]=[]
                golden=stock(raw,'golden',module,seed);roi=stock(raw,'roi',module,seed,width,height)
                for item in [golden,roi]:audit.append(item)
                old=dict(golden['writes']+roi['writes']);new=dict(sections['CONFIG'])
                # EN's engine bit was stopped first; FIFO/setup writes are unordered across layers.
                old[0xc]&=~1
                assert old==new,(module,seed,old,new)
                old=stock(raw,'start',module,seed);audit.append(old)
                expected=dict(old['writes']);actual=dict(sections['START'])
                assert actual[0x24]==(expected[0x24]&~4)|(4 if module else 0)
                assert actual[0xc]==expected[0xc]&~0xc0000
                # The native IRQ policy enables errors plus requested vblank, not frame-start.
                assert actual[4]==4|0x2000|(((1<<(2 if module else 4))-1)<<5)
                assert [event for event in sections['RESET'] if event[0]==0x14]==stock(raw,'reset',module,seed)['writes']
                sbch=[event for event in sections['RESET'] if event[0] in (0x3a0,0x3a4)]
                assert all(sbch==case['writes'][-2:] for case in cleanup)
                assert not any(event[0]==0x14 for event in sections['CONFIG'])
                fixtures+=1
        statuses=[0,0x7fff,0xffffffff,0x2064]+[1<<bit for bit in range(15)]
        for status in statuses:
            for late in [0,0x206]:audit.append(stock(raw,'irq',module,0,status=status,late=late))
    spec=importlib.util.spec_from_file_location('validate',ROOT/'scripts/validate-kernel.py')
    validate=importlib.util.module_from_spec(spec);spec.loader.exec_module(validate)
    nodes=validate.fdt_nodes(args.dtb.read_bytes())
    shipped=validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    for addr,part in [('1400b000','ovl0'),('1400c000','ovl0_2l')]:
        native=nodes['/soc/ovl@'+addr];reference=shipped['/disp_'+part+'@'+addr]
        assert native['reg']==reference['reg'] and native['interrupts']==reference['interrupts']
        compatible='mediatek,mt6765-disp-ovl'+('-2l' if part.endswith('_2l') else '')
        assert native['compatible']==compatible.encode()+b'\0'
        drv=(SRC/'drivers/gpu/drm/mediatek/mtk_drm_drv.c').read_text()
        assert re.search(r'\.compatible = "'+compatible+r'",\s*\.data = \(void \*\)MTK_DISP_OVL'+('_2L' if part.endswith('_2l') else '')+r'\s*}',drv)
    (ROOT/'out/mt6765-ovl-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    (ROOT/'out/mt6765-sbch-audit.json').write_text(json.dumps({
        'stock_image_sha256':hashlib.sha256(raw).hexdigest(),
        'fragment_entry':0x6fed38,'fragment_end':0x6ffd28,'cases':sbch_audit},indent=2)+'\n')
    print(f'PASS: {fixtures} native setup fixtures match stock register values; 76 stock IRQ traces classify frame completion and errors')
    print('PASS: reset polling/timeouts, clock/probe failures, inherited layers off, queued writes, bounds, IRQ lifetime and MT8192 regression')
    print('PASS: 36 stock SBCH cleanup fixtures; both native blocks clear reuse state after reset and before IRQ/start, including repeated power cycles')
    print('PASS: 65,536 status/callback combinations per native block, late status, vblank masks and error accounting; native DT resources match stock')
    print('MMIO side effects, clocks, IRQ synchronization and kernel services are modeled. No pixel flow or hardware boot is established.')


if __name__=='__main__':check()
