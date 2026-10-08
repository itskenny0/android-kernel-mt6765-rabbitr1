#!/usr/bin/env python3
"""Exercise native DSI ownership through the production CRTC startup callbacks."""
import argparse
import importlib.util
from pathlib import Path
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline/drivers/gpu/drm/mediatek'
spec = importlib.util.spec_from_file_location('command', Path(__file__).with_name('test-dsi-command.py'))
command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(command)

MODEL = r'''
struct drm_atomic_state { int unused; };
static struct mtk_dsi *dev_get_drvdata(struct device *dev)
{ assert(dev==dsi.host.dev);return &dsi; }
static struct mtk_dsi *bridge_to_dsi(struct drm_bridge *bridge)
{ assert(bridge==&dsi.bridge);return &dsi; }
'''

CRTC = r'''
#define MTK_MAX_BPC 10
#define drm_err(dev,...) ((void)(dev))
#define drm_dbg_driver(dev,...) ((void)(dev))
#define DRM_DEV_ERROR(dev,...) ((void)(dev))
struct drm_display_mode { unsigned int hdisplay,vdisplay; };
struct drm_crtc_state { struct drm_display_mode adjusted_mode; };
struct drm_device { struct device *dev; };
struct drm_crtc { struct drm_crtc_state *state;struct drm_device *dev; };
struct drm_encoder { struct drm_crtc *crtc; };
struct drm_connector { struct drm_encoder *encoder;struct { unsigned int bpc; } display_info; };
struct drm_connector_list_iter { int unused; };
struct cmdq_pkt;
struct mtk_plane_state { struct { bool enable; } pending; };
struct drm_plane { struct mtk_plane_state *state; };
struct mtk_mutex { int unused; };
struct mtk_ddp_comp_funcs {
    int (*clk_enable)(struct device *);
    void (*clk_disable)(struct device *);
    void (*start)(struct device *);
    void (*stop)(struct device *);
    unsigned int (*encoder_index)(struct device *);
};
struct mtk_ddp_comp { struct device *dev;unsigned int id;const struct mtk_ddp_comp_funcs *funcs; };
struct mtk_crtc {
    struct drm_crtc base;struct mtk_ddp_comp *ddp_comp[10];int ddp_comp_nr,layer_nr;
    struct drm_plane *planes;struct mtk_mutex *mutex;struct device *mmsys_dev;bool enabled;
};
static struct mtk_crtc crtc;
static struct device domain_dev,comp_devs[10];
static struct drm_device drm={.dev=&domain_dev};
static struct drm_crtc_state state={.adjusted_mode={480,640}};
static struct drm_encoder fixture_encoder;
static struct drm_connector fixture_connector={.encoder=&fixture_encoder,.display_info={8}};
static struct mtk_mutex disp_mutex;
static struct mtk_ddp_comp comps[10];
static struct drm_plane planes[1];
static struct mtk_plane_state plane_state;
static int fail_comp,fail_stage,domain_refs,main_refs,mutex_refs;
static unsigned int clock_mask,route_calls,mutex_adds,mutex_enables,config_calls,start_calls;
static unsigned int vblank_ons,layer_calls,bg_calls,update_calls,disable_order[10],disable_count;
#define drm_for_each_encoder(it,dev) for((it)=&fixture_encoder;(it);(it)=NULL)
#define drm_for_each_connector_iter(it,iter) for((it)=&fixture_connector;(it);(it)=NULL)
static void drm_connector_list_iter_begin(struct drm_device *dev,struct drm_connector_list_iter *it)
{ assert(dev==&drm);(void)it; }
static void drm_connector_list_iter_end(struct drm_connector_list_iter *it) { (void)it; }
static unsigned int drm_mode_vrefresh(const struct drm_display_mode *mode)
{ assert(mode==&state.adjusted_mode);return 60; }
static int pm_runtime_resume_and_get(struct device *dev)
{ assert(dev==&domain_dev);if(fail_stage==2)return -EAGAIN;main_refs++;return 0; }
static void pm_runtime_put(struct device *dev) { assert(dev==&domain_dev && main_refs==1);main_refs--; }
static int mtk_mutex_prepare(struct mtk_mutex *m)
{ assert(m==&disp_mutex);if(fail_stage==3)return -ENODEV;mutex_refs++;return 0; }
static void mtk_mutex_unprepare(struct mtk_mutex *m) { assert(m==&disp_mutex && mutex_refs==1);mutex_refs--; }
static int other_clk_enable(struct device *dev)
{
    unsigned int i=dev-comp_devs;assert(i<10 && !(clock_mask&BIT(i)));
    if((int)i==fail_comp)return -EBUSY;
    clock_mask|=BIT(i);return 0;
}
static void other_clk_disable(struct device *dev)
{
    unsigned int i=dev-comp_devs;assert(i<10 && (clock_mask&BIT(i)));
    clock_mask&=~BIT(i);assert(disable_count<10);disable_order[disable_count++]=i;
}
static void other_start(struct device *dev) { assert(clock_mask&BIT(dev-comp_devs)); }
static unsigned int mtk_dsi_encoder_index(struct device *dev) { assert(dev==dsi.host.dev);return 0; }
static int mtk_ddp_comp_power_on(struct mtk_ddp_comp *comp)
{ assert(comp==&comps[0]);if(fail_stage==1)return -EACCES;domain_refs++;return 0; }
static void mtk_ddp_comp_power_off(struct mtk_ddp_comp *comp)
{ assert(comp==&comps[0] && domain_refs==1);domain_refs--; }
static bool mtk_ddp_comp_connect(struct mtk_ddp_comp *comp,struct device *dev,unsigned int next)
{ (void)comp;(void)dev;(void)next;return false; }
static void mtk_mmsys_ddp_connect(struct device *dev,unsigned int first,unsigned int next)
{ (void)dev;assert(next==first+1);route_calls++; }
static bool mtk_ddp_comp_add(struct mtk_ddp_comp *comp,struct mtk_mutex *m)
{ (void)comp;(void)m;return false; }
static void mtk_mutex_add_comp(struct mtk_mutex *m,unsigned int id)
{ assert(m==&disp_mutex && id<10);mutex_adds++; }
static void mtk_mutex_enable(struct mtk_mutex *m)
{ assert(m==&disp_mutex && mutex_refs==1);mutex_enables++; }
static void mtk_ddp_comp_bgclr_in_on(struct mtk_ddp_comp *comp) { assert(comp==&comps[1]);bg_calls++; }
static void mtk_ddp_comp_config(struct mtk_ddp_comp *comp,unsigned int w,unsigned int h,
    unsigned int hz,unsigned int bpc,struct cmdq_pkt *cmdq)
{ (void)comp;assert(w==480 && h==640 && hz==60 && bpc==8 && !cmdq);config_calls++; }
static struct mtk_plane_state *to_mtk_plane_state(struct mtk_plane_state *s) { return s; }
static struct mtk_ddp_comp *mtk_ddp_comp_for_plane(struct drm_crtc *c,struct drm_plane *p,unsigned int *local)
{ assert(c==&crtc.base && p==planes);*local=0;return &comps[0]; }
static void mtk_ddp_comp_layer_config(struct mtk_ddp_comp *c,unsigned int local,
    struct mtk_plane_state *s,struct cmdq_pkt *cmdq)
{ assert(c==&comps[0] && !local && s==&plane_state && !s->pending.enable && !cmdq);layer_calls++; }
static struct mtk_crtc *to_mtk_crtc(struct drm_crtc *c) { assert(c==&crtc.base);return &crtc; }
static void mtk_crtc_update_output(struct drm_crtc *c,struct drm_atomic_state *s)
{ assert(c==&crtc.base && !s);update_calls++; }
static void drm_crtc_vblank_on(struct drm_crtc *c) { assert(c==&crtc.base);vblank_ons++; }
'''

TESTS = r'''
static void init_pipeline(unsigned int index)
{
    probe();memset(&crtc,0,sizeof(crtc));
    fail_comp=-1;fail_stage=domain_refs=main_refs=mutex_refs=0;
    clock_mask=route_calls=mutex_adds=mutex_enables=config_calls=start_calls=0;
    vblank_ons=layer_calls=bg_calls=update_calls=disable_count=0;
    crtc.base.dev=&drm;crtc.base.state=&state;
    crtc.ddp_comp_nr=10;crtc.layer_nr=1;crtc.planes=planes;crtc.mutex=&disp_mutex;
    fixture_encoder.crtc=&crtc.base;planes[0].state=&plane_state;plane_state.pending.enable=true;
    static const struct mtk_ddp_comp_funcs others={.clk_enable=other_clk_enable,
        .clk_disable=other_clk_disable,.start=other_start};
    for(unsigned int i=0;i<10;i++) {
        comps[i]=(struct mtk_ddp_comp){.dev=i==index?dsi.host.dev:&comp_devs[i],
            .id=i,.funcs=i==index?&ddp_dsi:&others};
        crtc.ddp_comp[i]=&comps[i];
    }
}
static void bridge_attempt(void)
{
    mtk_dsi_bridge_atomic_pre_enable(&dsi.bridge,NULL);
    mtk_dsi_bridge_atomic_enable(&dsi.bridge,NULL);
}
static void failed(void)
{
    assert(!crtc.enabled && !vblank_ons && !layer_calls && !route_calls && !mutex_adds);
    assert(!config_calls && !start_calls && !mutex_enables && !bg_calls);
    assert(!domain_refs && !main_refs && !mutex_refs && !clock_mask);
    assert(!dsi.refcount && !dsi.ddp_powered && !dsi.ddp_started && !dsi.bridge_powered);
    unsigned int before=mmio_count,clocks=clock_calls;
    bridge_attempt();mtk_dsi_bridge_atomic_disable(&dsi.bridge,NULL);
    mtk_dsi_bridge_atomic_post_disable(&dsi.bridge,NULL);
    mtk_dsi_ddp_stop(dsi.host.dev);mtk_dsi_ddp_clk_disable(dsi.host.dev);
    assert(!dsi.refcount && !dsi.enabled && mmio_count==before && clock_calls==clocks);
    assert(!engine_on && !digital_on && !phy_on && irq_depth==1 && !warnings);
}
static void success(void)
{
    assert(crtc.enabled && domain_refs==1 && main_refs==1 && mutex_refs==1);
    assert(vblank_ons==1 && layer_calls==1 && route_calls==9 && mutex_adds==10);
    assert(config_calls==10 && start_calls==10 && mutex_enables==1 && bg_calls==1);
    assert(dsi.refcount==1 && dsi.ddp_powered && dsi.ddp_started && !dsi.bridge_powered);
    unsigned int clocks=clock_calls,before_bridge=mmio_count;
    mtk_dsi_bridge_atomic_enable(&dsi.bridge,NULL);
    assert(!dsi.enabled && mmio_count==before_bridge);
    assert(!mtk_dsi_ddp_clk_enable(dsi.host.dev) && dsi.refcount==1);
    bridge_attempt();assert(dsi.refcount==2 && dsi.bridge_powered && dsi.enabled);
    bridge_attempt();assert(dsi.refcount==2 && clock_calls==clocks);
    u8 data=0x11;struct mipi_dsi_msg msg={.type=MIPI_DSI_DCS_SHORT_WRITE,.tx_buf=&data,.tx_len=1};
    mtk_dsi_bridge_atomic_disable(&dsi.bridge,NULL);
    /* CRTC stop precedes bridge post-disable; panel unprepare still needs DSI. */
    mtk_dsi_ddp_stop(dsi.host.dev);assert(dsi.refcount==1 && !dsi.ddp_started && !dsi.ddp_powered);
    mtk_crtc_ddp_clk_disable(&crtc);assert(!clock_mask && dsi.refcount==1);
    assert(mtk_dsi_host_transfer(&dsi.host,&msg)==1);
    mtk_dsi_bridge_atomic_enable(&dsi.bridge,NULL);assert(!dsi.enabled);
    mtk_dsi_bridge_atomic_post_disable(&dsi.bridge,NULL);
    assert(!dsi.refcount && !dsi.bridge_powered && !engine_on && irq_depth==1);
    unsigned int before=mmio_count;
    mtk_dsi_bridge_atomic_post_disable(&dsi.bridge,NULL);mtk_dsi_ddp_clk_disable(dsi.host.dev);
    assert(mmio_count==before && !dsi.refcount && !warnings);
    mtk_mutex_unprepare(&disp_mutex);pm_runtime_put(&domain_dev);mtk_ddp_comp_power_off(&comps[0]);
    crtc.enabled=false;
}
int main(int argc,char **argv)
{
    (void)argv;if(argc==2)return command_suite();
    unsigned int cases=0;
    for(unsigned int index=0;index<10;index++) {
        for(unsigned int fail=1;fail<=9;fail++) {
            init_pipeline(index);
            if(fail<=3)fail_stage=fail;
            else if(fail<=6)fail_clock=fail-3;
            else if(fail==7)fail_phy=1;
            else if(fail==8)fail_ulps=true;
            else dsi.lanes=0;
            mtk_crtc_atomic_enable(&crtc.base,NULL);failed();
            if(fail>3) {
                assert(disable_count==index);
                for(unsigned int i=0;i<index;i++)assert(disable_order[i]==index-i-1);
            }
            disable_count=0;fail_stage=fail_clock=fail_phy=0;fail_ulps=false;dsi.lanes=2;
            mtk_crtc_atomic_enable(&crtc.base,NULL);success();off();cases++;
        }
        for(unsigned int other=0;other<10;other++)if(other!=index) {
            init_pipeline(index);fail_comp=other;
            mtk_crtc_atomic_enable(&crtc.base,NULL);failed();
            assert(disable_count==other-(other>index));
            unsigned int n=0;
            for(int i=(int)other-1;i>=0;i--)if((unsigned int)i!=index)assert(disable_order[n++]==(unsigned int)i);
            disable_count=0;fail_comp=-1;mtk_crtc_atomic_enable(&crtc.base,NULL);success();off();cases++;
        }
    }
    /* Merely acquiring clocks must not permit bridge startup before DDP start. */
    init_pipeline(9);assert(!mtk_dsi_ddp_clk_enable(dsi.host.dev));
    unsigned int before=mmio_count;bridge_attempt();
    assert(dsi.refcount==1 && !dsi.bridge_powered && !dsi.enabled && mmio_count==before);
    mtk_dsi_ddp_clk_disable(dsi.host.dev);off();
    /* Stop without a bridge reference powers down before other clocks are dropped. */
    init_pipeline(9);mtk_crtc_atomic_enable(&crtc.base,NULL);
    mtk_dsi_ddp_stop(dsi.host.dev);assert(!dsi.refcount && !engine_on && clock_mask);
    mtk_crtc_ddp_clk_disable(&crtc);assert(!clock_mask && !warnings);off();
    /* An existing reference isolates legacy callback ownership from PHY setup. */
    power();dsi.driver_data=&mt8183_dsi_driver_data;
    before=mmio_count;assert(!mtk_dsi_ddp_clk_enable(dsi.host.dev));
    assert(dsi.refcount==1 && !dsi.ddp_powered && mmio_count==before);
    mtk_dsi_ddp_start(dsi.host.dev);assert(dsi.refcount==2 && !dsi.ddp_started);
    mtk_dsi_bridge_atomic_pre_enable(&dsi.bridge,NULL);
    assert(dsi.refcount==3 && !dsi.bridge_powered);
    dsi.lock.held=true;mtk_dsi_bridge_atomic_enable(&dsi.bridge,NULL);dsi.lock.held=false;
    assert(dsi.enabled);
    mtk_dsi_bridge_atomic_disable(&dsi.bridge,NULL);assert(!dsi.enabled);
    mtk_dsi_ddp_stop(dsi.host.dev);assert(dsi.refcount==2);
    mtk_dsi_bridge_atomic_post_disable(&dsi.bridge,NULL);assert(dsi.refcount==1);
    before=mmio_count;mtk_dsi_ddp_clk_disable(dsi.host.dev);
    assert(dsi.refcount==1 && mmio_count==before && !warnings);
    dsi.driver_data=&mt6765_dsi_driver_data;off();
    printf("PASS: %u CRTC startup failures and retries; DSI/bridge ownership, unwind and panel-unprepare access\n",cases);
    puts("PASS: legacy DDP and bridge callback reference order; new clock callbacks are no-ops");
    puts("Production CRTC acquisition and DSI callbacks; other components, DRM services and hardware are modeled.");
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'mtk_dsi.c')
    parser.add_argument('--comp-source', type=Path, default=SRC/'mtk_ddp_comp.c')
    args = parser.parse_args()
    for path in vars(args).values():
        if not path.resolve().is_relative_to(ROOT):
            raise SystemExit('Sources must stay under /rabbitr1')
    source = args.source.read_text()
    callbacks = ('mtk_dsi_bridge_atomic_disable', 'mtk_dsi_bridge_atomic_enable',
                 'mtk_dsi_bridge_atomic_pre_enable', 'mtk_dsi_bridge_atomic_post_disable',
                 'mtk_dsi_ddp_clk_enable', 'mtk_dsi_ddp_clk_disable',
                 'mtk_dsi_ddp_start', 'mtk_dsi_ddp_stop')
    extra = MODEL+''.join(command.block(source, name) for name in callbacks)+CRTC
    extra += command.block(args.comp_source.read_text(), 'ddp_dsi')
    header = (SRC/'mtk_ddp_comp.h').read_text()
    for name in ('mtk_ddp_comp_clk_enable', 'mtk_ddp_comp_clk_disable', 'mtk_ddp_comp_start'):
        body = command.block(header, name)
        if name == 'mtk_ddp_comp_start':
            body = body.replace('{\n', '{\n\tstart_calls++;\n', 1)
        extra += body
    crtc = (SRC/'mtk_crtc.c').read_text()
    for name in ('mtk_crtc_ddp_clk_enable', 'mtk_crtc_ddp_clk_disable',
                 'mtk_crtc_ddp_hw_init', 'mtk_crtc_atomic_enable'):
        extra += command.block(crtc, name)
    binary = command.build_harness(args.source, extra+TESTS, stem='dsi-pipeline-host')
    subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
