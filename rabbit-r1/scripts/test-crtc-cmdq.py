#!/usr/bin/env python3
"""Run the real CRTC producer, GCE submission/IRQ/stop and DRM event helpers.

Extends the event harness with the controller; MMIO, scheduling, locks, DMA and
PM are models. It does not establish physical bus quiescence or panel behavior.
"""
import importlib.util
from pathlib import Path
import re
import subprocess

ROOT=Path('/rabbitr1')
SRC=ROOT/'src/mainline'
HERE=Path(__file__).resolve().parent

def module(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

events=module('events','test-crtc-events.py')
flush=module('flush','test-cmdq-flush.py')


def lists(text):
    text=re.sub(r'\blist_', 'cmdq_list_',text)
    return text.replace('INIT_LIST_HEAD','CMDQ_INIT_LIST_HEAD')


def function(source,name):
    match=re.search(r'^static [^;\n]*\b'+name+r'\([^;]*?\)\s*\{',source,re.M)
    assert match,name
    depth=1;end=match.end()
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[match.start():end]


EXTRA_TYPES=r'''
#include <stdlib.h>
typedef uint8_t u8;
typedef uint64_t dma_addr_t;
#define __iomem
#define CMDQ_INST_SIZE 8
#define CMDQ_OP_CODE_SHIFT 24
#define MBOX_NO_MSG ((void *)-1)
#define EXPORT_SYMBOL_GPL(x)
#define dev_err(dev,...) ((void)(dev))
#define dev_err_ratelimited(dev,...) ((void)(dev))
#define GFP_ATOMIC 0
#define IS_ALIGNED(x,a) (!((x)&((a)-1)))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define smp_mb() ((void)0)
#define readl_poll_timeout_atomic(addr,val,cond,delay,timeout) ({ \
    int ret=-ETIMEDOUT;for(int tries=0;tries<4;tries++){val=readl(addr);if(cond){ret=0;break;}}ret; })
struct clk_bulk_data;
static void *dev_get_drvdata(struct device *);
static void transport_init(void);
'''

MODEL=r'''
static struct cmdq controller;
static struct cmdq_thread thread;
static struct gce_plat platform;
static u32 regs[0x1000/4];
static u64 packet_memory[512];
static unsigned int task_allocations,stop_sleeps,recover_after,stops,submissions;
static bool fail_alloc,fail_reset,fail_suspend,inject_callback,cpu_owned;
static void transport_irq(void);
#define queue_pending (!cmdq_list_empty(&thread.task_busy_list))
static void *dev_get_drvdata(struct device *d) { assert(d==&gce_dev);return &controller; }
static u32 readl(void *address) {
    assert(pm_refs>0);
    size_t offset=(char *)address-(char *)regs;
    assert(offset<sizeof(regs) && !(offset&3));return *(u32 *)address;
}
static void writel(u32 value,void *address) {
    assert(pm_refs>0 && channel.lock.held);
    size_t offset=(char *)address-(char *)regs;
    assert(offset<sizeof(regs) && !(offset&3));
    if(offset==CMDQ_THR_BASE+CMDQ_THR_IRQ_STATUS){*(u32 *)address&=value;return;}
    if(offset==CMDQ_THR_BASE+CMDQ_THR_ENABLE_TASK && value)assert(!cpu_owned);
    *(u32 *)address=value;
    if(offset==CMDQ_THR_BASE+CMDQ_THR_SUSPEND_TASK){
        if(value && !fail_suspend)regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_STATUS)/4]|=CMDQ_THR_STATUS_SUSPENDED;
        if(!value)regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_STATUS)/4]&=~CMDQ_THR_STATUS_SUSPENDED;
    }
    if(offset==CMDQ_THR_BASE+CMDQ_THR_WARM_RESET && value && !fail_reset)
        memset((char *)regs+CMDQ_THR_BASE,0,CMDQ_THR_SIZE);
}
static void *cmdq_allocate(size_t size) {
    if(fail_alloc)return NULL;
    void *p=calloc(1,size);assert(p);task_allocations++;return p;
}
#define kzalloc_obj(obj,flags) cmdq_allocate(sizeof(obj))
static void cmdq_free(void *p) { assert(task_allocations);task_allocations--;free(p); }
static void msleep(unsigned int ms) {
    assert(ms==20 && !lock_depth && pm_refs>0 && queue_pending && fixture.cmdq_pending);
    assert(!completions[0].done && physical_on);
    stop_sleeps++;assert(stop_sleeps<4);
    if(stop_sleeps==recover_after){queue_pm_fail=false;fail_reset=false;}
}
static void mbox_chan_received_data(struct mbox_chan *chan,void *message) {
    struct cmdq_cb_data *data=message;
    assert(chan==&channel && chan->lock.held);
    assert(data->pkt==&fixture.cmdq_handle);
    /* This producer has one packet: the last callback must see a stopped engine. */
    assert(!regs[(CMDQ_THR_BASE+CMDQ_THR_ENABLE_TASK)/4]);
    assert(!regs[(CMDQ_THR_BASE+CMDQ_THR_WARM_RESET)/4]);
    chan->cl->rx_callback(chan->cl,message);
}
static void transport_unlocked(struct lock *l) {
    if(l==&channel.lock && inject_callback && queue_pending){
        inject_callback=false;transport_irq();
    }
}
'''

HELPERS=r'''
static void transport_init(void) {
    assert(!task_allocations);
    memset(&controller,0,sizeof(controller));memset(&thread,0,sizeof(thread));
    memset(&platform,0,sizeof(platform));memset(regs,0,sizeof(regs));
    stop_sleeps=recover_after=stops=submissions=0;fail_alloc=fail_reset=fail_suspend=inject_callback=cpu_owned=false;
    controller.mbox=mailbox;controller.pdata=&platform;controller.base=regs;
    CMDQ_INIT_LIST_HEAD(&thread.task_busy_list);
    thread.base=(char *)regs+CMDQ_THR_BASE;thread.chan=&channel;
    channel.con_priv=&thread;channel.cl=&fixture.cmdq_client.client;
    channel.active_req=MBOX_NO_MSG;channel.msg_count=0;channel.lock=(struct lock){.rank=0};
    fixture.cmdq_client.client.rx_callback=ddp_cmdq_cb;
    fixture.cmdq_handle.va_base=packet_memory;fixture.cmdq_handle.pa_base=0x40000000;
}
static void transport_irq(void) {
    if(!queue_pending)return;
    regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_ADDR)/4]=queue_fail?fixture.cmdq_handle.pa_base:fixture.cmdq_handle.pa_base+fixture.cmdq_handle.cmd_buf_size;
    regs[(CMDQ_THR_BASE+CMDQ_THR_IRQ_STATUS)/4]=queue_fail?CMDQ_THR_IRQ_ERROR:CMDQ_THR_IRQ_DONE;
    spin_lock(&channel.lock);cmdq_thread_irq_handler(&controller,&thread);spin_unlock(&channel.lock);
}
static void service(void) {
    if(!deliver)return;
    mtk_crtc_ddp_irq(&fixture.base);transport_irq();
}
static unsigned int packet_destroyed;
static void cmdq_pkt_destroy(struct cmdq_client *client,struct cmdq_pkt *pkt) {
    assert(client==&fixture.cmdq_client && pkt==&fixture.cmdq_handle);
    assert(!queue_pending && !fixture.cmdq_pending && !pm_refs && !task_allocations);
    packet_destroyed++;
}
static void mbox_free_channel(struct mbox_chan *chan) {
    assert(chan==&channel && packet_destroyed);
    cmdq_mbox_shutdown(chan);chan->cl=NULL;
}
static void mtk_mutex_put(struct mtk_mutex *mutex) { (void)mutex;assert(!queue_pending); }
static void mtk_ddp_comp_unregister_vblank_cb(struct mtk_ddp_comp *comp) { (void)comp; }
static void drm_crtc_cleanup(struct drm_crtc *crtc) { assert(crtc==&fixture.base && packet_destroyed); }
'''

TESTS=r'''
static void begin_packet(void) {
    init(2,1);deliver=false;
    mtk_crtc_atomic_enable(&fixture.base,&transaction);
    arm(0,true);mtk_crtc_atomic_begin(&fixture.base,&transaction);
}
static void end_case(void) {
    fail_alloc=fail_reset=fail_suspend=queue_pm_fail=false;deliver=true;
    stop(7,false);assert(!task_allocations && !queue_pending && !fixture.cmdq_pending);
}
int main(void) {
    setvbuf(stdout,NULL,_IONBF,0);
    event_scenarios();
    unsigned int cases=0;
    for(unsigned int failure=0;failure<5;failure++) {
        begin_packet();
        if(failure==0)fail_alloc=true;
        if(failure==1)fail_reset=true;
        if(failure==2)queue_pm_fail=true;
        if(failure==3)controller.suspended=true;
        if(failure==4)fixture.cmdq_handle.buf_size=4;
        mtk_crtc_atomic_flush(&fixture.base,&transaction);done(0);
        assert(!queue_pending && !fixture.cmdq_pending && !pm_refs && !task_allocations);
        assert(!channel.msg_count && channel.active_req==MBOX_NO_MSG);
        assert(current.pending_config && plane_state.pending.config==false);
        controller.suspended=false;fixture.cmdq_handle.buf_size=4096;end_case();cases++;
    }
    for(unsigned int error=0;error<2;error++) {
        begin_packet();inject_callback=true;queue_fail=error;
        mtk_crtc_atomic_flush(&fixture.base,&transaction);done(0);
        assert(!fixture.cmdq_pending && !pm_refs && !task_allocations && !inject_callback);
        queue_fail=false;end_case();cases++;
    }
    for(unsigned int error=0;error<2;error++) {
        begin_packet();mtk_crtc_atomic_flush(&fixture.base,&transaction);
        assert(queue_pending && fixture.cmdq_pending && fixture.cmdq_page_flip==&events[0]);
        arm(1,false);mtk_crtc_atomic_begin(&fixture.base,&transaction);
        /* Old completion sees a new CRTC event, but must complete only its own. */
        queue_fail=error;transport_irq();done(0);
        assert(!completions[1].done && fixture.event==&events[1] && vb.refcount==1);
        queue_fail=false;mtk_crtc_atomic_flush(&fixture.base,&transaction);transport_irq();done(1);
        end_case();cases++;
    }
    begin_packet();mtk_crtc_atomic_flush(&fixture.base,&transaction);
    for(unsigned int i=0;i<4;i++)mtk_crtc_ddp_irq(&fixture.base);
    assert(!fixture.cmdq_vblank_cnt && fixture.cmdq_pending && queue_pending && pm_refs==1);
    end_case();done(0);cases++;
    for(unsigned int failure=0;failure<2;failure++) {
        begin_packet();mtk_crtc_atomic_flush(&fixture.base,&transaction);
        if(failure==0)fail_reset=true;else queue_pm_fail=true;
        recover_after=2;cmdq_mbox_stop(&channel);done(0);
        assert(stop_sleeps==2 && !pm_refs && !task_allocations);
        end_case();cases++;
    }
    begin_packet();mtk_crtc_atomic_flush(&fixture.base,&transaction);
    arm(1,false);mtk_crtc_atomic_begin(&fixture.base,&transaction);
    mtk_crtc_atomic_flush(&fixture.base,&transaction);done(0);
    assert(!completions[1].done && fixture.cmdq_page_flip==&events[1] && queue_pending);
    transport_irq();done(1);end_case();cases++;
    for(unsigned int rejection=0;rejection<2;rejection++) {
        begin_packet();plane_state.pending.enable=false;plane_state.pending.dirty=true;
        fail_alloc=rejection;
        mtk_crtc_plane_disable(&fixture.base,&fixture_plane);
        assert(!queue_pending && !fixture.cmdq_pending && !pm_refs);
        assert(layer_writes>=2 && !plane_state.pending.config);
        fail_alloc=false;mtk_crtc_atomic_flush(&fixture.base,&transaction);transport_irq();done(0);
        end_case();cases++;
    }
    begin_packet();mtk_crtc_atomic_flush(&fixture.base,&transaction);
    mtk_crtc_destroy(&fixture.base);done(0);
    assert(!fixture.cmdq_client.chan && packet_destroyed==1 && !pm_refs && !task_allocations);
    /* The surrounding DRM teardown owns display hardware/state, outside destroy. */
    cases++;
    printf("PASS: %u additional real CRTC/GCE ownership scenarios; MMIO, scheduling and PM modeled\n",cases);
    return 0;
}
'''


def build():
    src=SRC/'drivers/gpu/drm/mediatek/mtk_crtc.c'
    base=events.build(src,1).with_suffix('.c').read_text()
    driver=(SRC/'drivers/mailbox/mtk-cmdq-mailbox.c').read_text()
    header=(SRC/'include/linux/mailbox/mtk-cmdq-mailbox.h').read_text()
    list_types=flush.TYPES[flush.TYPES.index('struct list_head'):flush.TYPES.index('typedef struct { bool held; }')]
    base=base.replace('struct mbox_client { int unused; };','struct mbox_client { void (*rx_callback)(struct mbox_client *,void *); };')
    base=base.replace('struct drm_plane { struct drm_plane_state *state;', 'struct drm_plane { unsigned int index;struct drm_plane_state *state;')
    base=base.replace('struct mbox_chan { struct mbox_controller *mbox; };','struct mbox_chan { struct mbox_controller *mbox;struct mbox_client *cl;void *con_priv,*active_req;unsigned int msg_count;spinlock_t lock; };')
    packet='struct cmdq_pkt { unsigned int cmd_buf_size,buf_size;u64 pa_base; };'
    base=base.replace(packet,flush.block(header,'cmdq_mbox_priv')+flush.block(header,'cmdq_pkt'))
    base=base.replace('struct cmdq_client {',EXTRA_TYPES+lists(list_types)+'struct cmdq_client {',1)
    base=base.replace('static void unlock(struct lock *l)', 'static void transport_unlocked(struct lock *);\nstatic void unlock(struct lock *l)',1)
    base=base.replace('lock_depth--;l->held=false; }','lock_depth--;l->held=false;transport_unlocked(l); }',1)
    # Remove modeled transport; keep PM model and packet-builder helpers.
    for name in ('cmdq_mbox_stop','cmdq_mbox_send','service'):
        body=function(events.HELPERS if name=='service' else events.MODEL,name)
        assert base.count(body)==1,name
        base=base.replace(body,'')
    base=base.replace('static unsigned int queued,queue_pending,pm_refs,domain_refs,main_refs;','static unsigned int queued,pm_refs,domain_refs,main_refs;')
    base=base.replace('queued=queue_pending=pm_refs=domain_refs=main_refs=0;','queued=pm_refs=domain_refs=main_refs=0;')
    base=base.replace('!base && len==4096','base==0x40000000 && len==fixture.cmdq_handle.buf_size').replace('!base && len==8','base==0x40000000 && len==8')
    base=base.replace('dir==DMA_TO_DEVICE && !queue_pending);','dir==DMA_TO_DEVICE && !queue_pending);cpu_owned=true;')
    base=base.replace('base==0x40000000 && len==8 && dir==DMA_TO_DEVICE);',
        'base==0x40000000 && len==8 && dir==DMA_TO_DEVICE && cpu_owned && len<=fixture.cmdq_handle.buf_size);cpu_owned=false;')
    base=base.replace('(void)q;(void)ev;hw();','(void)q;(void)ev;assert(cpu_owned);hw();')
    base=base.replace('(void)q;(void)ev;assert(!clear);hw();','(void)q;(void)ev;assert(cpu_owned && !clear);hw();')
    base=base.replace('q->cmd_buf_size=8;hw();','assert(cpu_owned);q->cmd_buf_size=8;hw();')
    base=base.replace('assert(c==&fixture && physical_on);physical_on=false;','assert(c==&fixture && physical_on && !queue_pending);physical_on=false;')
    base=base.replace('fixture.cmdq_handle.buf_size=4096;','fixture.cmdq_handle.buf_size=4096;transport_init();')
    code=driver[driver.index('#define CMDQ_MBOX_AUTOSUSPEND_DELAY_MS'):driver.index('struct cmdq_thread')]
    code+=''.join(flush.block(driver,n) for n in ('cmdq_thread','cmdq_task','cmdq','gce_plat'))
    code=lists(code)+MODEL
    base=base.replace('static void hw(void)',code+'\nstatic void hw(void)',1)
    defs=''.join(flush.block(driver,n) for n in ('cmdq_convert_gce_addr','cmdq_revert_gce_addr',
        'cmdq_thread_suspend','cmdq_thread_resume','cmdq_thread_reset','cmdq_thread_disable',
        'cmdq_thread_invalidate_fetched_data','cmdq_task_insert_into_thread','cmdq_task_exec_done',
        'cmdq_thread_irq_handler','cmdq_mbox_send_data','cmdq_mbox_send','cmdq_mbox_stop','cmdq_mbox_shutdown'))
    defs=lists(defs).replace('kfree(', 'cmdq_free(')
    base=base.replace('static void drm_crtc_vblank_on(struct drm_crtc *c)\n{',defs+'\nstatic void drm_crtc_vblank_on(struct drm_crtc *c)\n{',1)
    # Supply real controller definitions before the CRTC producer calls them.
    base=base.replace('#define for_each_oldnew_crtc_in_state',HELPERS+'\n#define for_each_oldnew_crtc_in_state',1)
    base+='\n'+flush.block(src.read_text(),'mtk_crtc_destroy')+flush.block(src.read_text(),'mtk_crtc_plane_disable')
    base=base.replace('int main(void)', 'static int event_scenarios(void)',1)+TESTS
    base=base.replace('Core on/off, hardware, scheduling, other components and mailbox transport are modeled.',
        'Core on/off, hardware, scheduling and other components are modeled; transport uses the production GCE controller.')
    directory=ROOT/'out/crtc-cmdq-tests';directory.mkdir(exist_ok=True)
    path=directory/'ownership.c';path.write_text(base);binary=path.with_suffix('')
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-Wno-unused-but-set-variable','-Wno-sign-compare','-DTEST_CMDQ=1',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',str(path),'-o',str(binary)],check=True)
    return binary


if __name__=='__main__':
    subprocess.run([str(build())],check=True)
