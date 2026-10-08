#!/usr/bin/env python3
"""Compare MT6765 DSI setup callbacks with selected shipped kernel instructions."""
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
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X30)

ROOT=Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
os.environ['TMPDIR']=str(ROOT/'.tmp')
source=ROOT/'src/mainline/drivers/gpu/drm/mediatek/mtk_dsi.c'
if len(sys.argv)==2:
    source=Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')


def stock_setup(raw,params,function,end,width=480,height=640,seed=0x1234,
                conti_seed=0x7654a5a5,trace=False):
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    bias,obj,mmio,stop=0x1000000,0x20000000,0x30000000,0x40000000
    uc.mem_map(bias,0x2000000); uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000); uc.mem_map(mmio,0x1000); uc.mem_map(stop,0x1000)
    uc.mem_write(obj,bytes(params))
    uc.mem_write(mmio+0x64,struct.pack('<I',seed))
    uc.mem_write(mmio+0x90,struct.pack('<I',conti_seed))
    # DSI_REG[0] is zero-initialized BSS, normally set during host probe.
    uc.mem_write(bias+0x1a06960,struct.pack('<Q',mmio))
    for reg,value in [(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop),
            (UC_ARM64_REG_X0,13),(UC_ARM64_REG_X1,0),(UC_ARM64_REG_X2,obj),
            (UC_ARM64_REG_X3,width),(UC_ARM64_REG_X4,height)]:
        uc.reg_write(reg,value)
    writes=[]
    def code(uc,address,size,_):
        at=address-bias
        if at in (0x1f01c,0x747728,0xa5e2c,0x420ad0):
            # _mcount, dprec_logger_pr, printk, __dynamic_pr_debug.
            if at != 0x1f01c: uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert function<=at<end,hex(at)
    def write(uc,access,address,size,value,_):
        if mmio<=address<mmio+0x1000:
            assert size==4
            writes.append((address-mmio,value))
        else:
            assert obj<=address and address+size<=obj+0x10000,hex(address)
    uc.hook_add(UC_HOOK_CODE,code); uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+function,stop,count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop
    assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert bytes(uc.mem_read(obj,len(params)))==bytes(params)
    assert struct.unpack('<I',uc.mem_read(mmio+0x64,4))[0]==seed
    return writes if trace else dict(writes)


prelude=r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "/rabbitr1/src/mainline/include/video/mipi_display.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define FIELD_PREP(m,v) (((u32)(v)<<__builtin_ctz(m)) & (m))
#define FIELD_MAX(m) ((m)>>__builtin_ctz(m))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define DIV_ROUND_UP_ULL(n,d) DIV_ROUND_UP(n,d)
#define roundup(n,d) (DIV_ROUND_UP(n,d)*(d))
#define ALIGN(n,d) roundup(n,d)
#define max_t(t,a,b) ((t)(a)>(t)(b)?(t)(a):(t)(b))
#define HZ_PER_MHZ 1000000
#define NSEC_PER_SEC 1000000000
#define div_u64(n,d) ((u64)(n)/(d))
#define div64_u64(n,d) ((u64)(n)/(d))
#define fallthrough __attribute__((fallthrough))
#define DRM_WARN(...) ((void)0)
#define dev_err(dev,...) ((void)(dev))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
struct videomode { u64 pixelclock; u32 hactive,hfront_porch,hsync_len,hback_porch;
    u32 vactive,vfront_porch,vsync_len,vback_porch; };
struct drm_display_mode { int clock,hdisplay,hsync_start,hsync_end,htotal;
    int vdisplay,vsync_start,vsync_end,vtotal; };
enum drm_mode_status { MODE_OK,MODE_ERROR,MODE_CLOCK_LOW,MODE_CLOCK_HIGH,MODE_BAD_VVALUE,MODE_BAD_HVALUE };
struct device { int unused; };
struct mipi_dsi_host { struct device *dev; };
struct drm_bridge { int unused; };
struct drm_display_info { int unused; };
'''
structs=r'''
struct of_device_id { const char *compatible; const void *data; };
struct device_node { const char *compatible; };
static const struct of_device_id *of_match_node(const struct of_device_id *ids,
    const struct device_node *node)
{
    for (;ids->compatible;ids++) if (!strcmp(ids->compatible,node->compatible)) return ids;
    return NULL;
}
struct mtk_dsi { u8 *regs; int format; unsigned int lanes; unsigned long mode_flags;
    u32 data_rate; struct videomode vm; struct mtk_phy_timing phy_timing;
    const struct mtk_dsi_driver_data *driver_data;
    struct mipi_dsi_host host; struct drm_bridge bridge;
    int refcount, lock, irq, irq_data; void *phy,*hs_clk,*engine_clk,*digital_clk; bool lanes_ready;
};
static u8 regs[0x400];
static unsigned int writes,clk_sets,phy_ons,clock_ons;
static unsigned int conti_writes,lane_resets,lane_delays;
static int fail_clock,fail_phy;
static u32 readl(const void *p) { u32 v; memcpy(&v,p,4); return v; }
static void writel(u32 v,void *p) {
    assert((u8 *)p>=regs && (u8 *)p+4<=regs+sizeof(regs));
    if ((u8 *)p==regs+0x90) conti_writes++;
    memcpy(p,&v,4); writes++;
}
static int mipi_dsi_pixel_format_to_bpp(int format)
{ const int bpp[]={24,24,18,16,30}; return format>=0 && format<5?bpp[format]:-EINVAL; }
static struct mtk_dsi *bridge_to_dsi(struct drm_bridge *b) { return container_of(b,struct mtk_dsi,bridge); }
static int clk_set_rate(void *clk,u32 rate) { (void)clk; assert(rate>=125000000 && rate<=1500000000); clk_sets++; return fail_clock; }
static int phy_power_on(void *phy) { (void)phy; phy_ons++; return fail_phy; }
static void phy_power_off(void *phy) { (void)phy; }
static int clk_prepare_enable(void *clk) { (void)clk; clock_ons++; return 0; }
static void clk_disable_unprepare(void *clk) { (void)clk; }
static void mtk_dsi_enable(struct mtk_dsi *d) { (void)d; }
static void mtk_dsi_reset_engine(struct mtk_dsi *d) { (void)d; }
static void mtk_dsi_stop(struct mtk_dsi *d) { (void)d; }
static void mtk_dsi_set_cmd_mode(struct mtk_dsi *d) { (void)d; }
static void mtk_dsi_set_vm_cmd(struct mtk_dsi *d) { (void)d; }
static void mtk_dsi_set_interrupt_enable(struct mtk_dsi *d) { (void)d; }
static void mutex_lock(int *m) { assert(!*m); *m=1; }
static void mutex_unlock(int *m) { assert(*m); *m=0; }
static void atomic_set(int *p,int v) { *p=v; }
static void enable_irq(int irq) { (void)irq; }
static void mtk_dsi_reset_dphy(struct mtk_dsi *d) {
    assert(d->lanes_ready);
    if (d->driver_data->mt6765_regs) assert(readl(regs+0x90)==0x3c);
    lane_resets++;
}
static void mtk_dsi_clk_ulp_mode_leave(struct mtk_dsi *d) { assert(d->lanes_ready); }
static void mtk_dsi_lane0_ulp_mode_leave(struct mtk_dsi *d) { assert(d->lanes_ready); }
static void usleep_range(unsigned int lo,unsigned int hi) {
    assert((lo==30 && hi==100) || (lo==1000 && hi==3000)); lane_delays++;
}
static void mtk_dsi_clk_hs_mode(struct mtk_dsi *d,int hs) { (void)d; assert(hs==0 || hs==1); }
'''
main=r'''
static void check_modes(struct mtk_dsi *d,struct drm_display_mode good)
{
    assert(mt6765_dsi_mode_valid(d,&good)==MODE_OK);
    struct drm_display_mode m=good;
    m.clock=0; assert(mt6765_dsi_mode_valid(d,&m)==MODE_CLOCK_LOW);
    m.clock=1000; assert(mt6765_dsi_mode_valid(d,&m)==MODE_CLOCK_LOW);
    m.clock=2000000000; assert(mt6765_dsi_mode_valid(d,&m)==MODE_CLOCK_HIGH);
    m=good; m.vdisplay=0; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_VVALUE);
    m=good; m.vsync_end=m.vsync_start; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_VVALUE);
    m=good; m.vtotal=m.vsync_end-1; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_VVALUE);
    m=good; m.vtotal=m.vsync_end+4096; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_VVALUE);
    m=good; m.hsync_start=m.hdisplay; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_HVALUE);
    m=good; m.hsync_end=m.hsync_start+1; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_HVALUE);
    m=good; m.htotal=m.hsync_end; assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_HVALUE);
    m=good; m.hsync_start+=2000; m.hsync_end+=2000; m.htotal+=2000;
    assert(mt6765_dsi_mode_valid(d,&m)==MODE_BAD_HVALUE);
    d->format=4; assert(mt6765_dsi_mode_valid(d,&good)==MODE_ERROR); d->format=0;
    d->lanes=0; assert(mt6765_dsi_mode_valid(d,&good)==MODE_ERROR); d->lanes=5;
    assert(mt6765_dsi_mode_valid(d,&good)==MODE_ERROR); d->lanes=2;
    puts("modes:pass");
}
static const struct mtk_dsi_driver_data *match(const char *name)
{
    for (unsigned int i=0;mtk_dsi_of_match[i].compatible;i++)
        if (!strcmp(name,mtk_dsi_of_match[i].compatible)) return mtk_dsi_of_match[i].data;
    assert(false); return NULL;
}
int main(int argc,char **argv)
{
    assert(argc==18); u32 conti_seed=strtoul(argv[17],NULL,0);
    struct mtk_dsi d={ .regs=regs };
    struct device_node node={ .compatible="mediatek,mt6765-dsi" };
    enum mtk_ddp_comp_type component;
    assert(mtk_drm_of_get_ddp_comp_type(&node,&component)==0 && component==MTK_DSI);
    d.driver_data=match(atoi(argv[1])?"mediatek,mt6765-dsi":"mediatek,mt8183-dsi");
    d.data_rate=strtoul(argv[2],NULL,0); d.format=atoi(argv[3]); d.lanes=atoi(argv[4]);
    int mode=atoi(argv[5]);
    d.mode_flags=MIPI_DSI_MODE_VIDEO;
    if (mode==1 || mode==4) d.mode_flags|=MIPI_DSI_MODE_VIDEO_SYNC_PULSE;
    if (mode==3 || mode==4) d.mode_flags|=MIPI_DSI_MODE_VIDEO_BURST;
    if (atoi(argv[6])) d.mode_flags|=MIPI_DSI_CLOCK_NON_CONTINUOUS;
    d.vm.hactive=atoi(argv[7]); d.vm.hsync_len=atoi(argv[8]);
    d.vm.hback_porch=atoi(argv[9]); d.vm.hfront_porch=atoi(argv[10]);
    d.vm.vactive=atoi(argv[11]); d.vm.vsync_len=atoi(argv[12]);
    d.vm.vback_porch=atoi(argv[13]); d.vm.vfront_porch=atoi(argv[14]);
    if (atoi(argv[15])) configure_r1_panel(&d);
    struct drm_display_mode dm={ .clock=DIV_ROUND_UP_ULL((u64)d.data_rate*d.lanes,
        mipi_dsi_pixel_format_to_bpp(d.format)*1000),.hdisplay=d.vm.hactive,
        .hsync_start=d.vm.hactive+d.vm.hfront_porch,
        .hsync_end=d.vm.hactive+d.vm.hfront_porch+d.vm.hsync_len,
        .htotal=d.vm.hactive+d.vm.hfront_porch+d.vm.hsync_len+d.vm.hback_porch,
        .vdisplay=d.vm.vactive,.vsync_start=d.vm.vactive+d.vm.vfront_porch,
        .vsync_end=d.vm.vactive+d.vm.vfront_porch+d.vm.vsync_len,
        .vtotal=d.vm.vactive+d.vm.vfront_porch+d.vm.vsync_len+d.vm.vback_porch };
    if (atoi(argv[16])) {
        check_modes(&d,dm);
        const u64 clocks[]={0,1000000,200000000,380000000};
        for (unsigned int clock=0;clock<sizeof(clocks)/sizeof(clocks[0]);clock++) {
            d.vm.pixelclock=clocks[clock];
            assert(mtk_dsi_poweron(&d)==-EINVAL && d.refcount==0);
            assert(!clk_sets && !phy_ons && !clock_ons && !writes);
        }
        d.vm.pixelclock=21667000; d.format=-1;
        assert(mtk_dsi_poweron(&d)==-EINVAL && d.refcount==0);
        d.format=0; fail_clock=-EIO;
        assert(mtk_dsi_poweron(&d)==-EIO && d.refcount==0 && clk_sets==1 && !phy_ons);
        fail_clock=0; fail_phy=-EIO;
        assert(mtk_dsi_poweron(&d)==-EIO && d.refcount==0 && phy_ons==1 && !clock_ons && !writes);
        fail_phy=0; phy_ons=0;
        memcpy(regs+0x90,&conti_seed,4);
        assert(mtk_dsi_poweron(&d)==0 && d.refcount==1 && phy_ons==1 && clock_ons==2);
        assert(conti_writes==1 && lane_resets==1 && lane_delays==2 && readl(regs+0x90)==0x3c);
        unsigned int before=writes;
        assert(mtk_dsi_poweron(&d)==0 && d.refcount==2 && phy_ons==1 && clock_ons==2);
        mtk_dsi_lane_ready(&d);
        assert(writes==before && conti_writes==1 && lane_resets==1 && lane_delays==2);
        /* Model controller-state loss before a later lane initialization. */
        d.lanes_ready=false;memcpy(regs+0x90,&conti_seed,4);mtk_dsi_lane_ready(&d);
        assert(conti_writes==2 && lane_resets==2 && lane_delays==4 && readl(regs+0x90)==0x3c);
        puts("power:pass"); return 0;
    }
    printf("status:%d\n",mtk_dsi_bridge_mode_valid(&d.bridge,NULL,&dm));
    writel(0x1234,regs+DSI_HSTX_CKL_WC);
    memcpy(regs+0x90,&conti_seed,4);
    mtk_dsi_phy_timconfig(&d); mtk_dsi_ps_control(&d,true);
    mtk_dsi_rxtx_control(&d); mtk_dsi_config_vdo_timing(&d);
    assert(conti_writes==d.driver_data->mt6765_regs);
    const unsigned int offsets[]={0x18,0x1c,0x20,0x24,0x28,0x2c,0x38,0x50,0x54,0x58,0x5c,0x64,0x90,0x110,0x114,0x118,0x11c};
    for (unsigned int i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++)
        printf("%x:%x\n",offsets[i],readl(regs+offsets[i]));
}
'''


def build_harness():
    s=source.read_text()
    def block(name,s=s):
        # All selected functions/structs have a closing brace at column zero.
        pattern=r'^.*\b'+re.escape(name)+r'(?:\(|\s*\{|\s*(?:\[\])?\s*=)'
        match=re.search(pattern,s,re.M); assert match,name
        start=match.start(); end=s.index('\n}',start)+2
        if s[end:end+1]==';': end+=1
        # Include the separate return-type line for the mode validators.
        if s[start:].startswith(name+'('): start=s.rfind('\n',0,start-1)+1
        return s[start:end]+'\n'
    header=(ROOT/'src/mainline/include/drm/drm_mipi_dsi.h').read_text()
    flags='\n'.join(re.findall(r'^#define MIPI_DSI_(?:MODE|CLOCK|HS_PKT)[^\n]*',header,re.M))+'\n'
    formats=header[header.index('enum mipi_dsi_pixel_format {'):header.index('};',header.index('enum mipi_dsi_pixel_format {'))+2]
    macros=s[s.index('#define DSI_START'):s.index('struct mtk_phy_timing')]
    functions=['mtk_dsi_phy_timing','mt6765_dsi_phy_timing','mtk_dsi_phy_timconfig',
        'mtk_dsi_rxtx_control','mtk_dsi_ps_control','mtk_dsi_config_vdo_timing_per_frame_lp',
        'mtk_dsi_config_vdo_timing_per_line_lp','mt6765_dsi_config_vdo_timing','mtk_dsi_config_vdo_timing',
        'mt6765_dsi_mode_valid','mtk_dsi_bridge_mode_valid','mtk_dsi_lane_ready','mtk_dsi_poweron',
        'mt2701_dsi_driver_data','mt8173_dsi_driver_data','mt6765_dsi_driver_data',
        'mt8183_dsi_driver_data','mt8186_dsi_driver_data','mt8188_dsi_driver_data','mtk_dsi_of_match']
    panel=(ROOT/'src/mainline/drivers/gpu/drm/panel/panel-rabbit-r1.c').read_text()
    settings=panel[panel.index('\tdsi->lanes ='):panel.index('\tmipi_dsi_set_drvdata')]
    code=prelude+flags+formats+'\n'+macros+block('mtk_phy_timing')+block('mtk_dsi_driver_data')+structs
    code+=''.join(block(x) for x in functions)
    directory=ROOT/'src/mainline/drivers/gpu/drm/mediatek'
    code+=block('mtk_ddp_comp_type',(directory/'mtk_ddp_comp.h').read_text())
    driver=(directory/'mtk_drm_drv.c').read_text()
    code+=block('mtk_ddp_comp_dt_ids',driver)+block('mtk_drm_of_get_ddp_comp_type',driver)
    code+='static void configure_r1_panel(struct mtk_dsi *dsi)\n{\n'+settings+'}\n'+main
    path=ROOT/'out/dsi-timing-host.c'; path.write_text(code)
    binary=ROOT/'out/dsi-timing-host'
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-sign-compare','-Wno-unused-parameter',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',str(path),'-o',str(binary)],check=True)
    return binary


def main_test():
    spec=importlib.util.spec_from_file_location('panel',ROOT/'scripts/test-r1-panel.py')
    panel=importlib.util.module_from_spec(spec); spec.loader.exec_module(panel)
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==panel.IMAGE_SHA256
    _,full=panel.stock_trace(raw,0x6eed08); original=full[472:]
    binary=build_harness()
    def host(rate=260000000,fmt=0,lanes=2,mode=1,nc=1,timing=(480,20,20,20,640,2,14,26),native=1,board=0,checks=0,
             conti_seed=0x7654a5a5):
        args=[native,rate,fmt,lanes,mode,nc,*timing,board,checks,conti_seed]
        lines=subprocess.check_output([str(binary),*map(str,args)],text=True).splitlines()
        if checks: assert lines==['modes:pass','power:pass']; return
        assert lines[0]=='status:0',lines[0]
        return {int(a,16):int(b,16) for a,b in (line.split(':') for line in lines[1:])}
    def params_for(rate=260,fmt=0,lanes=2,mode=1,nc=1,timing=(480,20,20,20,640,2,14,26)):
        data=bytearray(original)
        ha,hs,hb,hf,va,vs,vb,vf=timing
        for off,value in {0:mode,20:lanes,36:[3,1,2,0][fmt],44:[2,1,3,0][fmt],
                60:vs,64:vb,68:vf,76:va,80:hs,84:hb,88:hf,96:ha,196:0,204:rate,232:1-nc}.items():
            struct.pack_into('<I',data,off,value)
        return data
    rates=[125,126,200,250,260,261,400,500,750,1000,1250,1500]
    for rate in rates:
        expected=stock_setup(raw,params_for(rate=rate),0x716540,0x716c14)
        actual=host(rate=rate*1000000)
        assert {k:actual[k] for k in expected}==expected,(rate,actual,expected)
    # Actual panel-mode request is 260.004 Mbit/s; byte timings remain identical.
    exact=stock_setup(raw,original,0x716540,0x716c14)
    board=host(rate=260004000,board=1)
    assert {k:board[k] for k in exact}==exact
    assert board[0x64]==0x1234
    rxtx=stock_setup(raw,original,0x713024,0x71396c)
    assert {k:board[k] for k in rxtx}==rxtx
    assert struct.unpack_from('<I',original,232)[0]==0
    cases=0
    for timing in [(480,20,20,20,640,2,14,26),(640,17,31,27,960,3,16,29)]:
        for fmt in range(4):
            for mode in (1,2,3):
                params=params_for(fmt=fmt,mode=mode,timing=timing)
                expected=stock_setup(raw,params,0x711a60,0x7121b8)
                expected.update(stock_setup(raw,params,0x712a94,0x713024,timing[0],timing[4]))
                actual=host(fmt=fmt,mode=mode,timing=timing)
                assert {k:actual[k] for k in expected}==expected,(fmt,mode,actual,expected)
                cases+=1
    txrx_cases=[]
    for lanes in range(1,5):
        for nc in (0,1):
            for seed in (0,0xffffffff,0x7654a5a5):
                trace=stock_setup(raw,params_for(lanes=lanes,nc=nc),0x713024,0x71396c,conti_seed=seed,trace=True)
                assert [write for write in trace if write[0]==0x90]==[(0x90,0x3c)]*2
                expected=dict(trace);actual=host(lanes=lanes,nc=nc,conti_seed=seed)
                assert {k:actual[k] for k in expected}==expected
                assert host(lanes=lanes,nc=nc,native=0,conti_seed=seed)[0x90]==seed
                txrx_cases.append({'lanes':lanes,'noncontinuous_clock':nc,'seed':seed,'stock_writes':trace})
    assert host(mode=4)==host(mode=3), 'Burst mode must take precedence over sync-pulse flag'
    host(checks=1)
    # Known pre-change MT8183 values at 260.004 Mbit/s, r1-size mode.
    legacy=host(rate=260004000,native=0)
    assert [legacy[x] for x in (0x50,0x54,0x58,0x64)]==[50,29,34,1440]
    assert [legacy[x] for x in (0x110,0x114,0x118,0x11c)]==[0x04040302,0x05080406,0x02080100,0x00040a02]
    assert host(fmt=1,native=0)[0x1c]>>16==2 and host(fmt=2,native=0)[0x1c]>>16==1
    result={'stock_image_sha256':panel.IMAGE_SHA256,'dphy_rates_mbps':rates,'video_pixel_cases':cases,
            'lane_clock_cases':24,'txrx_cases':txrx_cases,'r1_registers':{hex(k):hex(v) for k,v in board.items()},
            'scope':'selected stock setup instructions and production callbacks with modeled MMIO; no PHY or panel hardware'}
    (ROOT/'out/dsi-timing-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS: 12 D-PHY rates, 24 video/pixel cases and 24 lane/clock/dirty-MEM_CONTI cases match shipped setup')
    print('PASS: native memory-continue programming through lane startup and repeated initialization; MT8183 preserves the register')
    print('PASS: r1 fractional rate, burst precedence, mode bounds, early power failures and MT8183 regression')
    print('No PLL, analog PHY, physical link or display pipeline was emulated or tested.')


if __name__=='__main__': main_test()
