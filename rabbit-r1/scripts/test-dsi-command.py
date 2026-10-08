#!/usr/bin/env python3
"""Exercise native DSI transactions, IRQ lifetime, and the shipped RX/RACK sequence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X25, UC_ARM64_REG_X29

ROOT = Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')


def block(s, name):
    m = re.search(r'^[a-zA-Z_][^\n]*\b'+re.escape(name)+r'(?:\(|\s*\{|\s*=)', s, re.M)
    assert m, name
    end = s.index('\n}', m.start())+2
    if s[end:end+1] == ';': end += 1
    return s[m.start():end]+'\n'


def stock_receive(raw, payload, rack):
    # Execute the actual CPU read path from its successful RD_RDY wait through RACK.
    # The scheduler and prior wait are outside this slice, not emulated successes.
    bias, obj, mmio = 0x1000000, 0x20000000, 0x30000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, 0x2000000); uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000); uc.mem_map(mmio, 0x1000)
    uc.mem_write(obj, struct.pack('<Q', mmio))
    uc.mem_write(mmio+0x74, payload); uc.mem_write(mmio+0x84, struct.pack('<I', rack))
    fp, sp = obj+0x8000, obj+0x7800
    uc.reg_write(UC_ARM64_REG_X25, obj); uc.reg_write(UC_ARM64_REG_X29, fp)
    uc.reg_write(UC_ARM64_REG_SP, sp)
    events = []
    def read(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            assert size == 4
            events.append(['read', address-mmio])
    def write(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            assert size == 4 and address == mmio+0x84 and value == rack|1
            events.append(['write', address-mmio, value])
        else:
            assert sp <= address and address+size <= fp
    uc.hook_add(UC_HOOK_MEM_READ, read); uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.emu_start(bias+0x718634, bias+0x718918, count=100)
    assert uc.reg_read(UC_ARM64_REG_PC) == bias+0x718918
    assert uc.reg_read(UC_ARM64_REG_SP) == sp
    copied = b''.join(bytes(uc.mem_read(fp-off, 4)) for off in (0x18, 0x1c, 0x20, 0x28))
    assert copied == payload
    assert events == [['read', off] for off in (0x74,0x78,0x7c,0x80,0x84)]+[['write',0x84,rack|1]]
    return dict(data=payload.hex(), rack_before=rack, events=events)


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <sys/types.h>
#include "/rabbitr1/src/mainline/include/video/mipi_display.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
typedef _Atomic(u32) atomic_t;
typedef int irqreturn_t;
#define ERESTARTSYS 512
#define IRQ_HANDLED 1
#define IRQ_NONE 0
#define IRQF_NO_AUTOEN 0x80000
#define DRM_MODE_CONNECTOR_DSI 16
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define DIV_ROUND_UP_ULL(n,d) DIV_ROUND_UP(n,d)
#define min(a,b) ((a)<(b)?(a):(b))
#define min_t(t,a,b) min((t)(a),(t)(b))
#define DRM_WARN(...) ((void)0)
#define DRM_ERROR(...) ((void)0)
#define DRM_INFO(...) ((void)0)
#define dev_err(dev,...) ((void)(dev))
#define WARN_ON(c) (c)
#define IS_ERR(p) ((uintptr_t)(p)>(uintptr_t)-4096)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define MIPI_DSI_MSG_USE_LPM BIT(1)
#define MIPI_DSI_MSG_REQ_ACK BIT(0)
#define msecs_to_jiffies(t) (t)
struct device { void *of_node; };
struct platform_device { struct device dev; };
struct mipi_dsi_host { struct device *dev; const void *ops; };
struct drm_bridge { void *of_node; int type; };
struct mipi_dsi_msg { u8 channel,type; unsigned int flags; size_t tx_len; const void *tx_buf;
    size_t rx_len; void *rx_buf; };
struct mipi_dsi_packet { size_t size; u8 header[4]; size_t payload_length; const u8 *payload; };
struct mutex { bool held; };
static void atomic_set(atomic_t *v,u32 n) { atomic_store(v,n); }
static u32 atomic_read(atomic_t *v) { return atomic_load(v); }
static void atomic_or(u32 bits,atomic_t *v) { atomic_fetch_or(v,bits); }
static void atomic_andnot(u32 bits,atomic_t *v) { atomic_fetch_and(v,~bits); }
static void mutex_init(struct mutex *m) { m->held=false; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held=true; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held=false; }
static u32 get_unaligned_le16(const u8 *p) { return p[0]|(u32)p[1]<<8; }
static void put_unaligned_le32(u32 v,u8 *p) { for(unsigned int i=0;i<4;i++) p[i]=v>>(8*i); }
'''
MODEL = r'''
struct mtk_dsi {
    u8 *regs;
    struct mipi_dsi_host host;
    struct drm_bridge bridge;
    const struct mtk_dsi_driver_data *driver_data;
    atomic_t irq_data;
    unsigned int irq_wait_queue;
    struct mutex lock;
    int irq,refcount,format;
    bool lanes_ready,enabled;
    unsigned int lanes;
    struct { u64 pixelclock; } vm;
    u32 data_rate;
    void *phy,*hs_clk,*engine_clk,*digital_clk;
};
static struct mtk_dsi dsi;
static u8 regs[0x1000];
static int hs,engine,digital,phy;
static bool engine_on,digital_on,phy_on,irq_requested,in_irq;
static unsigned int irq_depth,fail_clock,fail_phy,clock_calls,waits,polls,racks,rx_words;
static unsigned int starts,resets,mmio_count,registers,irq_requests,wakes;
static int fail_wait,wait_error,fail_poll,request_error,register_error;
static bool stale,extra_cmd,permanent_busy;
static u32 last_flags[8];
static unsigned int last_steps[8];
static const struct mtk_dsi_driver_data *platform_data;
static const int mtk_dsi_ops;
static irqreturn_t mtk_dsi_irq(int irq,void *p);
static int mtk_dsi_poweron(struct mtk_dsi *d);
static u32 load(unsigned int off) { u32 v; memcpy(&v,regs+off,4); return v; }
static void save(unsigned int off,u32 v) { memcpy(regs+off,&v,4); }
static void access_ok(void)
{
    assert(engine_on && digital_on && phy_on);
    assert(dsi.lock.held || in_irq);
    mmio_count++;
}
static void fire(u32 bits)
{
    assert(!irq_depth && irq_requested && engine_on && digital_on);
    save(0xc,load(0xc)|bits);
    in_irq=true;
    assert(mtk_dsi_irq(dsi.irq,&dsi)==(bits&0xb?IRQ_HANDLED:IRQ_NONE));
    in_irq=false;
}
static u32 readl(const void *p)
{
    access_ok(); unsigned int off=(const u8 *)p-regs;
    assert(off+4<=sizeof(regs));
    if (off>=0x74 && off<=0x80) { assert(!in_irq && !racks); rx_words++; }
    return load(off);
}
static void writel(u32 v,void *p)
{
    access_ok(); unsigned int off=(u8 *)p-regs;
    assert(off+4<=sizeof(regs));
    if (off==0xc) { save(off,load(off)&v); return; }
    if (off==0x84) {
        assert(!in_irq && (v&1)); racks++;
        assert(!rx_words || rx_words==4);
        if (!permanent_busy) save(0xc,load(0xc)&~0x80000000U);
    }
    if (off==0x10 && (v&1)) {
        resets++; save(0xc,load(0xc)&~0x80000000U);
    }
    if (off==0 && v==1 && !(load(0x14)&3)) {
        starts++; save(0xc,load(0xc)|0x80000000U);
    }
    save(off,v);
}
static void disable_irq(int irq)
{
    assert(irq==dsi.irq && irq_requested && dsi.lock.held);
    assert(engine_on && digital_on && !in_irq); irq_depth++;
}
static void enable_irq(int irq)
{
    assert(irq==dsi.irq && irq_requested && dsi.lock.held && irq_depth==1);
    assert(engine_on && digital_on && phy_on); irq_depth--;
    if (load(0xc)&load(0x8)&0xb) { stale=true; fire(load(0xc)&load(0x8)&0xb); }
}
static void wake_up_interruptible(unsigned int *q)
{ assert(q==&dsi.irq_wait_queue && in_irq); wakes++; }
static void init_waitqueue_head(unsigned int *q) { *q=1; }
static void step_wait(u32 flag,unsigned int step)
{
    assert(dsi.lock.held && !irq_depth);
    if (flag==1 && extra_cmd && step==0) { fire(2); return; }
    if (flag==2 && !permanent_busy) save(0xc,load(0xc)&~0x80000000U);
    fire(flag);
}
#define wait_event_interruptible_timeout(q,condition,t) ({ \
    assert((q)==1 && (t)>0 && waits<8); \
    unsigned int slot=waits++; last_flags[slot]=irq_flag; long result=0; \
    if ((int)waits==fail_wait) result=wait_error; \
    else for(unsigned int step=0;step<4;step++) { \
        if (condition) { result=1; break; } \
        step_wait(irq_flag,step); last_steps[slot]++; \
        if (condition) { result=1; break; } \
    } result; })
#define readl_poll_timeout(addr,val,condition,delay,timeout) ({ \
    assert((delay)==4 && (timeout)==2000000); polls++; \
    int result=-ETIMEDOUT; \
    for(unsigned int n=0;n<4;n++) { \
        val=readl(addr); \
        if ((int)polls==fail_poll) val|=0x80000000U; \
        if (condition) { result=0; break; } \
    } result; })
static int clk_set_rate(void *p,u32 rate) { assert(p==&hs && rate>=125000000); return fail_clock==1?-EIO:0; }
static int phy_power_on(void *p) { assert(p==&phy && !phy_on); if(fail_phy) return -EIO; phy_on=true; return 0; }
static void phy_power_off(void *p) { assert(p==&phy && phy_on && !engine_on && !digital_on); phy_on=false; }
static int clk_prepare_enable(void *p)
{
    assert(phy_on); clock_calls++;
    if (fail_clock==(p==&engine?2U:3U)) return -EIO;
    if (p==&engine) engine_on=true; else { assert(p==&digital && engine_on); digital_on=true; }
    return 0;
}
static void clk_disable_unprepare(void *p)
{
    assert(irq_depth && !in_irq && load(0x8)==0);
    if(p==&engine) engine_on=false; else { assert(p==&digital); digital_on=false; }
}
static int mipi_dsi_pixel_format_to_bpp(int format) { return format==0?24:-EINVAL; }
static void mtk_dsi_phy_timconfig(struct mtk_dsi *d) { assert(d==&dsi); access_ok(); }
static void mtk_dsi_ps_control(struct mtk_dsi *d,bool v) { assert(d==&dsi && v); access_ok(); }
static void mtk_dsi_set_vm_cmd(struct mtk_dsi *d) { assert(d==&dsi); access_ok(); }
static void mtk_dsi_config_vdo_timing(struct mtk_dsi *d) { assert(d==&dsi); access_ok(); }
static void mtk_dsi_lane_ready(struct mtk_dsi *d) { assert(d==&dsi); access_ok(); d->lanes_ready=true; }
static void mtk_dsi_clk_hs_mode(struct mtk_dsi *d,int hs_mode) { assert(d==&dsi && hs_mode==1); access_ok(); }
static void mtk_dsi_lane0_ulp_mode_enter(struct mtk_dsi *d) { assert(d==&dsi && irq_depth); access_ok(); }
static void mtk_dsi_clk_ulp_mode_enter(struct mtk_dsi *d) { assert(d==&dsi && irq_depth); access_ok(); }
static struct mtk_dsi *bridge_alloc(void) { return &dsi; }
#define devm_drm_bridge_alloc(dev,type,member,funcs) bridge_alloc()
static const void *of_device_get_match_data(struct device *dev) { (void)dev; return platform_data; }
static void *devm_clk_get(struct device *dev,const char *name)
{ (void)dev; return !strcmp(name,"engine")?&engine:!strcmp(name,"digital")?&digital:&hs; }
static void *devm_platform_ioremap_resource(struct platform_device *p,int index) { (void)p; assert(!index); return regs; }
static void *devm_phy_get(struct device *d,const char *name) { (void)d; assert(!strcmp(name,"dphy")); return &phy; }
static int platform_get_irq(struct platform_device *p,int index) { (void)p; assert(!index); return 73; }
static void platform_set_drvdata(struct platform_device *p,void *d) { (void)p; assert(d==&dsi); }
static struct mtk_dsi *platform_get_drvdata(struct platform_device *p) { (void)p; return &dsi; }
static const char *dev_name(struct device *dev) { (void)dev; return "dsi"; }
static int dev_err_probe(struct device *dev,int ret,const char *message) { (void)dev;(void)message; return ret; }
static int devm_request_irq(struct device *dev,int irq,irqreturn_t (*fn)(int,void *),unsigned long flags,
                           const char *name,void *p)
{
    (void)dev; (void)name; assert(irq==73 && p==&dsi && fn==mtk_dsi_irq && !registers);
    assert(flags==IRQF_NO_AUTOEN && dsi.irq_wait_queue==1 && !atomic_read(&dsi.irq_data));
    irq_requests++;
    if (request_error) return request_error;
    irq_requested=true; irq_depth=1; return 0;
}
static int mipi_dsi_host_register(struct mipi_dsi_host *h)
{
    assert(h==&dsi.host && irq_requested && irq_depth==1 && !mmio_count);
    assert(dsi.bridge.of_node==h->dev->of_node && dsi.bridge.type==DRM_MODE_CONNECTOR_DSI);
    registers++; return register_error;
}
static void mipi_dsi_host_unregister(struct mipi_dsi_host *h) { assert(h==&dsi.host && registers); registers--; }
static void mtk_dsi_set_mode(struct mtk_dsi *d) { writel(1,d->regs+DSI_MODE_CTRL); }
static struct mtk_dsi *host_to_dsi(struct mipi_dsi_host *h) { assert(h==&dsi.host); return &dsi; }
static u8 readb(const void *p) { (void)p; assert(0); return 0; }
static ssize_t mtk_dsi_host_send_cmd(struct mtk_dsi *d,const struct mipi_dsi_msg *m,u8 flag)
{ (void)d;(void)m;(void)flag;assert(0);return 0; }
static u32 mtk_dsi_recv_cnt(u8 type,u8 *data) { (void)type;(void)data;assert(0);return 0; }
'''
TESTS = r'''
static struct platform_device pdev={.dev={.of_node=&pdev}};
static void setup(void)
{
    memset(&dsi,0,sizeof(dsi)); memset(regs,0,sizeof(regs));
    engine_on=digital_on=phy_on=irq_requested=in_irq=false;
    irq_depth=fail_clock=fail_phy=clock_calls=waits=polls=racks=rx_words=starts=resets=0;
    mmio_count=registers=irq_requests=wakes=0;
    fail_wait=wait_error=fail_poll=request_error=register_error=0;
    stale=extra_cmd=permanent_busy=false;
    memset(last_flags,0,sizeof(last_flags)); memset(last_steps,0,sizeof(last_steps));
    platform_data=&mt6765_dsi_driver_data;
}
static void probe(void)
{
    setup(); assert(!mtk_dsi_probe(&pdev)); assert(!mmio_count && irq_depth==1);
    dsi.lanes=2; dsi.vm.pixelclock=20000000;
}
static void power(void)
{
    probe(); save(0xc,0xb); save(0x14,3); atomic_set(&dsi.irq_data,0xb);
    assert(!mtk_dsi_poweron(&dsi) && dsi.refcount==1 && !irq_depth && !stale);
    assert(!atomic_read(&dsi.irq_data) && !(load(0xc)&0xb) && load(0x8)==0xb && !load(0x14));
    resets=mmio_count=0;
}
static void off(void)
{
    mtk_dsi_remove(&pdev);
    assert(!dsi.refcount && !dsi.lanes_ready && !engine_on && !digital_on && !phy_on);
    assert(irq_depth==1 && !registers && !dsi.lock.held);
}
static unsigned int cases;
static void writes(void)
{
    u8 tx[509]; for(unsigned int n=0;n<sizeof(tx);n++) tx[n]=(n*37+0x80)&255;
    const u8 types[]={0x03,0x13,0x23,0x05,0x15,0x37,0x29,0x39};
    const unsigned int lens[]={0,1,2,1,2,2,508,508};
    for(unsigned int t=0;t<sizeof(types);t++) for(unsigned int ch=0;ch<4;ch++)
    for(unsigned int mode=0;mode<4;mode++) for(unsigned int lp=0;lp<2;lp++)
    for(size_t len=(t<6?lens[t]:0);len<=lens[t];len++) {
        power(); save(0x14,mode|0x10000); save(0xc,3); atomic_set(&dsi.irq_data,3);
        struct mipi_dsi_msg m={.type=types[t],.channel=ch,.flags=lp?2:0,.tx_buf=len?tx:NULL,.tx_len=len};
        assert(mtk_dsi_host_transfer(&dsi.host,&m)==(ssize_t)len);
        assert(!dsi.lock.held && !irq_depth && !rx_words && !racks);
        assert(load(0x14)==(mode|0x10000) && starts==1 && !resets && polls==2);
        assert(waits==1+!!mode && last_flags[!!mode]==2 && last_steps[!!mode]==1);
        u32 hdr=(u32)(ch<<6|types[t])<<8 | (lp?0:8);
        if(t>=6) hdr|=(u32)len<<16 | 2;
        else { if(len) hdr|=(u32)tx[0]<<16; if(len>1) hdr|=(u32)tx[1]<<24; }
        assert(load(0x200)==hdr && load(0x60)==1+(t>=6?(len+3)/4:0));
        if(t>=6) for(unsigned int n=0;n<(len+3)/4*4;n++)
            assert(regs[0x204+n]==(n<len?tx[n]:0));
        off(); cases++;
    }
}
static void reads(void)
{
    u8 tx[2]={0x80,0xff},rx[18],frame[16];
    const u8 reqs[]={0x04,0x14,0x24,0x06}, types[]={0x11,0x12,0x21,0x22,0x1a,0x1c};
    const unsigned int lens[]={0,1,2,1};
    const unsigned int counts[]={0,1,2,3,4,8,9,10,11,12,16,256,4096,65535};
    for(unsigned int r=0;r<4;r++) for(unsigned int t=0;t<sizeof(types);t++)
    for(unsigned int ch=0;ch<4;ch++) for(unsigned int mode=0;mode<4;mode++)
    for(unsigned int lp=0;lp<2;lp++)
    for(unsigned int co=0;co<sizeof(counts)/sizeof(counts[0]);co++) for(size_t rxlen=1;rxlen<=16;rxlen++) {
        power(); save(0x14,mode); extra_cmd=(co%2)==1;
        for(unsigned int n=0;n<16;n++) frame[n]=0xc0+n;
        frame[0]=ch<<6|types[t]; size_t count=t<4?(t%2+1):counts[co];
        if(t>=4) { frame[1]=count;frame[2]=count>>8; }
        memcpy(regs+0x74,frame,16); memset(rx,0xa5,sizeof(rx));
        struct mipi_dsi_msg m={.type=reqs[r],.channel=ch,.flags=lp?2:0,.tx_buf=lens[r]?tx:NULL,.tx_len=lens[r],
                              .rx_buf=rx+1,.rx_len=rxlen};
        size_t expected=min(rxlen,min(count,10));
        assert(mtk_dsi_host_transfer(&dsi.host,&m)==(ssize_t)expected);
        assert(!memcmp(rx+1,frame+(t<4?1:4),expected) && rx[0]==0xa5 && rx[expected+1]==0xa5);
        assert(rx_words==4 && racks==1 && !resets && waits==2+!!mode && starts==1 && polls==2);
        assert(last_flags[!!mode]==1 && last_flags[1+!!mode]==2);
        assert(last_steps[!!mode]==(extra_cmd?2U:1U)); /* CMD_DONE alone cannot complete a read. */
        assert(load(0x14)==mode && !irq_depth && !dsi.lock.held);
        assert((load(0x204)&0xffff)==((u32)(ch<<6|reqs[r])<<8|(lp?4:12)));
        assert(load(0x200)==((u32)(ch<<6|0x37)<<8|(lp?0:8)|(u32)min(rxlen,10)<<16));
        assert(load(0x60)==2);
        off(); cases++;
    }
}
static void failures(void)
{
    u8 tx[509]={0xa5},rx[16],data[16]={0};
    struct mipi_dsi_msg good={.type=0x06,.tx_buf=tx,.tx_len=1,.rx_buf=rx,.rx_len=sizeof(rx)},m;
    probe(); unsigned int before=mmio_count;
    assert(mtk_dsi_host_transfer(&dsi.host,&good)==-EHOSTDOWN && mmio_count==before && !dsi.lock.held);
    for(unsigned int bad=0;bad<11;bad++) {
        power(); m=good; int error=-EINVAL;
        switch(bad) {
        case 0:m.channel=4;break; case 1:m.tx_buf=NULL;break; case 2:m.rx_buf=NULL;break;
        case 3:m.rx_len=0;break; case 4:m.tx_len=2;break;
        case 5:m.flags=1;error=-EOPNOTSUPP;break; case 6:m.flags=4;error=-EOPNOTSUPP;break;
        case 7:m.type=0xff;m.rx_len=0;error=-EOPNOTSUPP;break;
        case 8:m.type=0x39;m.tx_len=509;m.rx_len=0;error=-EMSGSIZE;break;
        case 9:m.type=0x39;m.tx_len=SIZE_MAX;m.rx_len=0;error=-EMSGSIZE;break;
        case 10:m.type=0x05;break;
        }
        before=mmio_count;
        assert(mtk_dsi_host_transfer(&dsi.host,&m)==error && mmio_count==before && !dsi.lock.held); off();cases++;
    }
    for(unsigned int mode=0;mode<4;mode++) for(unsigned int rd=0;rd<2;rd++)
    for(unsigned int point=1;point<=1+rd+!!mode;point++) for(unsigned int signal=0;signal<2;signal++) {
        power(); save(0x14,mode); memset(rx,0xa5,sizeof(rx)); save(0x74,0x9c21);
        m=good; if(!rd) { m.type=0x05;m.rx_len=0; }
        fail_wait=point;wait_error=signal?-ERESTARTSYS:0;
        assert(mtk_dsi_host_transfer(&dsi.host,&m)==(signal?-ERESTARTSYS:-ETIMEDOUT));
        assert(rx[0]==0xa5 && resets==1 && !irq_depth && !dsi.lock.held && load(0x14)==mode);
        assert(waits==point && !(load(0xc)&0xb) && !atomic_read(&dsi.irq_data));
        assert(starts==!(mode && point==1));
        if(rd && starts) assert(racks==1); else assert(!racks);
        fail_wait=0;waits=polls=racks=rx_words=0;
        assert(mtk_dsi_host_transfer(&dsi.host,&good)==1 && rx[0]==0x9c);
        off();cases++;
    }
    for(unsigned int point=1;point<3;point++) {
        power(); fail_poll=point; memset(rx,0xa5,sizeof(rx));save(0x74,0x9c21);
        assert(mtk_dsi_host_transfer(&dsi.host,&good)==-ETIMEDOUT && rx[0]==0xa5 && resets==1);
        assert(starts==(point==2) && racks==(point==2));off();cases++;
    }
    power(); permanent_busy=true; memset(rx,0xa5,sizeof(rx));save(0x74,0x9c21);
    assert(mtk_dsi_host_transfer(&dsi.host,&good)==-ETIMEDOUT && waits==2 && polls==2 && racks==1 && rx_words==4);
    permanent_busy=false;off();cases++;
    for(unsigned int kind=0;kind<256;kind++) {
        if(kind==0x11 || kind==0x12 || kind==0x21 || kind==0x22 || kind==0x1a || kind==0x1c) continue;
        power(); data[0]=kind;memcpy(regs+0x74,data,16);memset(rx,0xa5,sizeof(rx));
        assert(mtk_dsi_host_transfer(&dsi.host,&good)==-EPROTO && rx[0]==0xa5 && racks==1);off();cases++;
    }
    for(unsigned int fail=1;fail<=4;fail++) {
        probe(); if(fail<4) fail_clock=fail; else fail_phy=1;
        assert(mtk_dsi_poweron(&dsi)==-EIO && !dsi.refcount && irq_depth==1);
        assert(!mmio_count && !engine_on && !digital_on && !phy_on && !dsi.lock.held);
        fail_clock=fail_phy=0;assert(!mtk_dsi_poweron(&dsi));off();cases++;
    }
    for(unsigned int lanes=0;lanes<=5;lanes+=5) {
        probe();dsi.lanes=lanes;
        assert(mtk_dsi_poweron(&dsi)==-EINVAL && !dsi.refcount && !mmio_count && !dsi.lock.held);cases++;
    }
    setup();request_error=-EBUSY;
    assert(mtk_dsi_probe(&pdev)==-EBUSY && !registers && irq_requests==1);cases++;
    setup();register_error=-EIO;
    assert(mtk_dsi_probe(&pdev)==-EIO && irq_requests==1 && !mmio_count);cases++;
}
static void lifetime(void)
{
    probe(); mtk_output_dsi_enable(&dsi); assert(!dsi.enabled && !mmio_count && !dsi.lock.held);
    power(); mtk_output_dsi_enable(&dsi); assert(dsi.enabled && load(0x14)==1 && !dsi.lock.held);
    unsigned int before=mmio_count; mtk_output_dsi_enable(&dsi); assert(mmio_count==before);
    mtk_output_dsi_disable(&dsi); assert(!dsi.enabled && !dsi.lock.held); off();cases++;
    power();unsigned int clocks=clock_calls;
    assert(!mtk_dsi_poweron(&dsi) && dsi.refcount==2 && clock_calls==clocks);
    mtk_dsi_poweroff(&dsi);assert(dsi.refcount==1 && !irq_depth && engine_on && !waits);
    mtk_dsi_poweroff(&dsi);assert(!dsi.refcount && irq_depth==1 && !engine_on && !digital_on && !waits);
    assert(!mtk_dsi_poweron(&dsi) && dsi.refcount==1 && !irq_depth && !stale);
    for(unsigned int bit=0;bit<3;bit++) {
        static const u32 events[]={1,2,8};
        dsi.lock.held=true;fire(events[bit]);mtk_dsi_irq_data_clear(&dsi,2);
        assert(atomic_read(&dsi.irq_data)==(bit==0?1:bit==1?1:9)); dsi.lock.held=false;
    }
    off();cases++;
    for(unsigned int mode=1;mode<4;mode++) for(unsigned int fail=0;fail<2;fail++) {
        power();save(0x14,mode);if(fail) fail_wait=1;
        off();assert(waits==1 && last_flags[0]==8);cases++;
    }
}
int main(void)
{
    failures();lifetime();writes();reads();
    printf("PASS: %u native DSI transactions and lifecycle cases\n",cases);
    puts("Production IRQ/waits/packets/probe/power paths; clocks, PHY, IRQ delivery and scheduler modeled.");
    return 0;
}
'''


def build_harness(source):
    s = source.read_text()
    macros = s[s.index('#define DSI_START'):s.index('struct mtk_phy_timing')]
    code = PRELUDE+macros+block(s,'mtk_dsi_driver_data')+MODEL
    shared = (ROOT/'src/mainline/drivers/gpu/drm/drm_mipi_dsi.c').read_text()
    for name in ('mipi_dsi_packet_format_is_short','mipi_dsi_packet_format_is_long','mipi_dsi_create_packet'):
        code += block(shared,name)
    for name in ('mtk_dsi_mask','mtk_dsi_enable','mtk_dsi_disable','mtk_dsi_reset_engine',
                 'mtk_dsi_start','mtk_dsi_stop','mtk_dsi_set_cmd_mode','mtk_dsi_set_interrupt_enable',
                 'mtk_dsi_irq_data_set','mtk_dsi_irq_data_clear','mtk_dsi_wait_for_irq_done','mtk_dsi_irq',
                 'mtk_dsi_switch_to_cmd_mode','mt6765_dsi_clear_irq','mt6765_dsi_enter_cmd_mode',
                 'mtk_dsi_poweron','mtk_dsi_poweroff','mtk_output_dsi_enable','mtk_output_dsi_disable','mt6765_dsi_validate_msg','mt6765_dsi_cmdq',
                 'mt6765_dsi_read_response','mt6765_dsi_transfer','mtk_dsi_host_transfer','mtk_dsi_probe','mtk_dsi_remove',
                 'mt6765_dsi_driver_data'):
        code += block(s,name)
    path = ROOT/'out/dsi-command-host.c'; path.write_text(code+TESTS)
    binary = ROOT/'out/dsi-command-host'
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-sign-compare','-Wno-unused-parameter',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',str(path),'-o',str(binary)],check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'src/mainline/drivers/gpu/drm/mediatek/mtk_dsi.c')
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must stay under /rabbitr1')
    subprocess.run([str(build_harness(args.source))],check=True)
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    fixtures = [stock_receive(raw,bytes((seed+n*37)&255 for n in range(16)),rack)
                for seed in (0,1,0x55,0xff) for rack in (0,1,0x12345678,0xffffffff)]
    (ROOT/'out/dsi-command-audit.json').write_text(json.dumps(dict(
        stock_slice='DSI_dcs_read_lcm_reg_v2 raw 0x718634..0x7186c0',
        fixtures=fixtures,hardware_tested=False),indent=2)+'\n')
    print(f'PASS: {len(fixtures)} shipped RX-copy-before-RACK sequences')


if __name__ == '__main__': main()
