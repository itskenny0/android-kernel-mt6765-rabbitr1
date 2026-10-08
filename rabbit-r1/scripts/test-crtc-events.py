#!/usr/bin/env python3
"""Run MediaTek commit/event callbacks with DRM's real vblank reference helpers."""
import argparse
import importlib.util
import os
from pathlib import Path
import resource
import subprocess

ROOT=Path('/rabbitr1')
SRC=ROOT/'src/mainline/drivers/gpu/drm'
spec=importlib.util.spec_from_file_location('command',Path(__file__).with_name('test-dsi-command.py'))
command=importlib.util.module_from_spec(spec);spec.loader.exec_module(command)
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
os.environ['TMPDIR']=str(ROOT/'.tmp')

TYPES=r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t ktime_t;
typedef int atomic_t;
typedef unsigned int wait_queue_head_t;
struct lock { int rank;bool held; };
typedef struct lock spinlock_t;
struct mutex { bool held; };
static unsigned int warnings,lock_depth,locks[8];
static void lock(struct lock *l) { assert(!l->held && lock_depth<8);assert(!lock_depth || locks[lock_depth-1]<l->rank);locks[lock_depth++]=l->rank;l->held=true; }
static void unlock(struct lock *l) { assert(l->held && lock_depth && locks[lock_depth-1]==l->rank);lock_depth--;l->held=false; }
#define spin_lock(l) lock(l)
#define spin_unlock(l) unlock(l)
#define spin_lock_irqsave(l,f) do { (f)=0;lock(l); } while(0)
#define spin_unlock_irqrestore(l,f) do { (void)(f);unlock(l); } while(0)
#define assert_spin_locked(l) assert((l)->held)
static void mutex_lock(struct mutex *m) { assert(!lock_depth && !m->held);m->held=true; }
static void mutex_unlock(struct mutex *m) { assert(m->held);m->held=false; }
#define IS_REACHABLE(x) TEST_CMDQ
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define BIT(n) (1U<<(n))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define WARN_ON(x) ({ bool bad=(x);warnings+=bad;bad; })
#define drm_WARN_ON(dev,x) WARN_ON(x)
#define drm_err(dev,...) ((void)(dev))
#define drm_dbg_driver(dev,...) ((void)(dev))
#define drm_dbg_core(dev,...) ((void)(dev))
#define DRM_DEV_ERROR(dev,...) ((void)(dev))
#define msecs_to_jiffies(x) (x)
#define DRIVER_MODESET 1
#define HZ 1000
#define DMA_TO_DEVICE 1
#define EPOLLIN 1
#define EPOLLRDNORM 2
#define DRM_EVENT_VBLANK 1
#define DRM_EVENT_FLIP_COMPLETE 2
#define DRM_EVENT_CRTC_SEQUENCE 3
#define DRM_PLANE_COMMIT_ACTIVE_ONLY 1
#define DRM_PLANE_COMMIT_NO_DISABLE_AFTER_MODESET 2
struct device { int id; };
struct completion { unsigned int done,released; };
struct fence { unsigned int signaled,put; };
struct list_head { bool linked; };
struct drm_file { struct list_head event_list;unsigned int event_wait; };
struct drm_pending_event {
    struct completion *completion;void (*completion_release)(struct completion *);
    struct fence *fence;struct drm_file *file_priv;
    struct list_head pending_link,link;bool freed;
};
struct drm_pending_vblank_event {
    struct drm_pending_event base;unsigned int pipe;
    struct { struct { int type; } base;struct { u64 sequence;long tv_sec,tv_usec; } vbl;
        struct { u64 sequence;ktime_t time_ns; } seq; } event;
};
struct timespec64 { long tv_sec,tv_nsec; };
struct drm_vblank_crtc {
    atomic_t refcount;bool enabled,inmodeset;
    struct { int offdelay_ms;bool disable_immediate; } config;int disable_timer;
};
struct drm_crtc;
struct drm_atomic_state;
struct drm_plane;
struct drm_plane_state { struct drm_crtc *crtc; };
struct drm_crtc_state { bool active,color_mgmt_changed,no_vblank;struct drm_pending_vblank_event *event; };
struct drm_crtc_funcs { int (*enable_vblank)(struct drm_crtc *); };
struct drm_crtc_helper_funcs {
    void (*atomic_begin)(struct drm_crtc *,struct drm_atomic_state *);
    void (*atomic_flush)(struct drm_crtc *,struct drm_atomic_state *);
};
struct drm_plane_helper_funcs {
    void (*atomic_disable)(struct drm_plane *,struct drm_atomic_state *);
    void (*atomic_update)(struct drm_plane *,struct drm_atomic_state *);
    void (*atomic_enable)(struct drm_plane *,struct drm_atomic_state *);
    void (*end_fb_access)(struct drm_plane *,struct drm_plane_state *);
};
struct drm_device { struct device *dev;void *dev_private;unsigned int num_crtcs;
    spinlock_t event_lock,vbl_lock,vblank_time_lock; };
struct drm_crtc { struct drm_device *dev;struct drm_crtc_state *state;
    const struct drm_crtc_funcs *funcs;const struct drm_crtc_helper_funcs *helper_private; };
struct drm_plane { struct drm_plane_state *state;const struct drm_plane_helper_funcs *helper_private; };
struct mtk_plane_state { struct drm_plane_state base;struct {
    bool enable,dirty,config,async_dirty,async_config;
} pending; };
struct mtk_ddp_comp { struct device *dev;unsigned int id; };
struct mtk_mutex { int unused; };
struct mbox_client { int unused; };
struct mbox_controller { struct device *dev; };
struct mbox_chan { struct mbox_controller *mbox; };
struct cmdq_client { struct mbox_client client;struct mbox_chan *chan; };
struct cmdq_pkt { unsigned int cmd_buf_size,buf_size;u64 pa_base; };
struct cmdq_cb_data { int sta;struct cmdq_pkt *pkt; };
struct mtk_mmsys_driver_data { bool shadow_register; };
struct mtk_drm_private { struct mtk_mmsys_driver_data *data; };
struct drm_atomic_state { struct drm_device *dev;struct drm_crtc_state *old_state,*new_state;
    struct drm_plane_state *old_plane,*new_plane;bool modeset; };
static int mtk_crtc_enable_vblank(struct drm_crtc *);
static void mtk_crtc_disable_vblank(struct drm_crtc *);
static void mtk_crtc_ddp_irq(void *);
static void service(void);
'''

MODEL=r'''
static struct mtk_crtc fixture;
static struct mtk_crtc_state current;
static struct drm_crtc_state previous;
static struct device display_dev={1},gce_dev={2};
static struct mtk_mmsys_driver_data soc;
static struct mtk_drm_private priv={.data=&soc};
static struct drm_device drm={.dev=&display_dev,.dev_private=&priv,.num_crtcs=1};
static struct drm_vblank_crtc vb;
static struct mtk_ddp_comp comp[2];
static struct mtk_ddp_comp *comp_ptrs[]={&comp[0],&comp[1]};
static struct mtk_plane_state plane_state;
static struct drm_plane_state old_plane;
static struct drm_plane fixture_plane;
static struct drm_atomic_state transaction;
static struct drm_pending_vblank_event events[8];
static struct completion completions[8];
static struct fence fences[8];
static struct drm_file files[8];
static struct mbox_controller mailbox={.dev=&gce_dev};
static struct mbox_chan channel={.mbox=&mailbox};
static unsigned int hw_writes,gamma_writes,ctm_writes,config_writes,layer_writes;
static unsigned int sent,put_refs,get_calls,ons,offs,irqs,updates,hw_done,cleanups;
static unsigned int queued,queue_pending,pm_refs,domain_refs,main_refs;
static unsigned int start_error,hw_error,enable_error;
static u64 sequence;
static bool physical_on,deliver,queue_fail,queue_pm_fail;
static void hw(void) { assert(physical_on);hw_writes++; }
static int atomic_read(atomic_t *a) { return *a; }
static int atomic_add_return(int n,atomic_t *a) { return *a+=n; }
static void atomic_dec(atomic_t *a) { (*a)--; }
static bool atomic_dec_and_test(atomic_t *a) { return --*a==0; }
static bool drm_dev_has_vblank(struct drm_device *d) { assert(d==&drm);return true; }
static bool drm_core_check_feature(struct drm_device *d,int flag) { assert(d==&drm && flag==DRIVER_MODESET);return true; }
static unsigned int drm_crtc_index(struct drm_crtc *c) { assert(c==&fixture.base);return 0; }
static struct drm_crtc *drm_crtc_from_index(struct drm_device *d,unsigned int pipe)
{ assert(d==&drm && !pipe);return &fixture.base; }
static struct drm_vblank_crtc *drm_vblank_crtc(struct drm_device *d,unsigned int pipe)
{ assert(d==&drm && !pipe);return &vb; }
static void drm_update_vblank_count(struct drm_device *d,unsigned int pipe,int unused)
{ assert(d==&drm && !pipe && !unused); }
static void vblank_disable_fn(int *timer)
{ assert(timer==&vb.disable_timer);if(vb.enabled)mtk_crtc_disable_vblank(&fixture.base);vb.enabled=false; }
static void mod_timer(int *timer,unsigned int time) { assert(timer==&vb.disable_timer && time); }
static const unsigned int jiffies=1;
static void complete_all(struct completion *c) { assert(!c->done);c->done++; }
static void release_completion(struct completion *c) { assert(c->done==1 && !c->released);c->released++; }
static void dma_fence_signal_timestamp(struct fence *f,ktime_t time) { assert(time && !f->signaled);f->signaled++; }
static void dma_fence_signal(struct fence *f) { assert(!f->signaled);f->signaled++; }
static void dma_fence_put(struct fence *f) { assert(f->signaled==1 && !f->put);f->put++; }
static void kfree(struct drm_pending_event *e) { assert(!e->freed);e->freed=true; }
static void list_del(struct list_head *l) { assert(l->linked);l->linked=false; }
static void list_add_tail(struct list_head *l,struct list_head *head) { (void)head;assert(!l->linked);l->linked=true; }
static void wake_up_interruptible_poll(unsigned int *q,unsigned int flags) { assert(flags==3);(*q)++; }
static struct timespec64 ktime_to_timespec64(ktime_t t) { return (struct timespec64){t/1000000000,t%1000000000}; }
static ktime_t ktime_to_ns(ktime_t t) { return t; }
static ktime_t ktime_get(void) { return 1234567890; }
static u64 drm_vblank_count_and_time(struct drm_device *d,unsigned int pipe,ktime_t *now)
{ assert(d==&drm && !pipe);*now=ktime_get();return sequence; }
static void trace_drm_vblank_event_delivered(struct drm_file *f,unsigned int pipe,u64 seq)
{ (void)f;assert(!pipe && seq==sequence);sent++; }
static struct mtk_plane_state *to_mtk_plane_state(struct drm_plane_state *s)
{ return container_of(s,struct mtk_plane_state,base); }
static struct drm_crtc_state *drm_atomic_get_new_crtc_state(struct drm_atomic_state *s,struct drm_crtc *c)
{ assert(s==&transaction && c==&fixture.base);return s->new_state; }
static void mtk_ddp_comp_enable_vblank(struct mtk_ddp_comp *c) { assert(c==&comp[0]);hw(); }
static void mtk_ddp_comp_disable_vblank(struct mtk_ddp_comp *c) { assert(c==&comp[0]);hw(); }
static int mtk_ddp_comp_power_on(struct mtk_ddp_comp *c)
{ assert(c==&comp[0]);if(start_error)return -EACCES;domain_refs++;return 0; }
static void mtk_ddp_comp_power_off(struct mtk_ddp_comp *c)
{ assert(c==&comp[0] && domain_refs==1);domain_refs--; }
static void mtk_crtc_update_output(struct drm_crtc *c,struct drm_atomic_state *s)
{ assert(c==&fixture.base && s==&transaction); }
static int mtk_crtc_ddp_hw_init(struct mtk_crtc *c)
{ assert(c==&fixture && !physical_on);if(hw_error)return -EIO;physical_on=true;main_refs++;return 0; }
static void mtk_ddp_comp_stop(struct mtk_ddp_comp *c) { (void)c;hw(); }
static void mtk_ddp_comp_bgclr_in_off(struct mtk_ddp_comp *c) { assert(c==&comp[1]);hw(); }
static bool mtk_ddp_comp_remove(struct mtk_ddp_comp *c,struct mtk_mutex *m) { (void)c;(void)m;return false; }
static void mtk_mutex_remove_comp(struct mtk_mutex *m,unsigned int id) { (void)m;assert(id<2);hw(); }
static void mtk_mutex_disable(struct mtk_mutex *m) { (void)m;hw(); }
static bool mtk_ddp_comp_disconnect(struct mtk_ddp_comp *c,struct device *d,unsigned int next)
{ (void)c;(void)d;(void)next;return false; }
static void mtk_mmsys_ddp_disconnect(struct device *d,unsigned int from,unsigned int to)
{ (void)d;assert(from==0 && to==1);hw(); }
static void mtk_crtc_ddp_clk_disable(struct mtk_crtc *c) { assert(c==&fixture && physical_on);physical_on=false; }
static void mtk_mutex_unprepare(struct mtk_mutex *m) { (void)m; }
static void pm_runtime_put(struct device *d) { assert(d==&display_dev && main_refs==1);main_refs--; }
static void mtk_ddp_comp_config(struct mtk_ddp_comp *c,unsigned int w,unsigned int h,
    unsigned int hz,unsigned int bpc,struct cmdq_pkt *q)
{ (void)c;(void)w;(void)h;(void)hz;(void)bpc;(void)q;hw();config_writes++; }
static struct mtk_ddp_comp *mtk_ddp_comp_for_plane(struct drm_crtc *c,struct drm_plane *p,unsigned int *local)
{ assert(c==&fixture.base && p==&fixture_plane);*local=0;return &comp[0]; }
static void mtk_ddp_comp_layer_config(struct mtk_ddp_comp *c,unsigned int i,
    struct mtk_plane_state *s,struct cmdq_pkt *q)
{ assert(c==&comp[0] && !i && s==&plane_state);(void)q;hw();layer_writes++; }
static void mtk_ddp_gamma_set(struct mtk_ddp_comp *c,struct drm_crtc_state *s)
{ (void)c;assert(s==&current.base);hw();gamma_writes++; }
static void mtk_ddp_ctm_set(struct mtk_ddp_comp *c,struct drm_crtc_state *s)
{ (void)c;assert(s==&current.base);hw();ctm_writes++; }
static void mtk_mutex_acquire(struct mtk_mutex *m) { (void)m;hw(); }
static void mtk_mutex_release(struct mtk_mutex *m) { (void)m;hw(); }
static void drm_crtc_handle_vblank(struct drm_crtc *c)
{ assert(c==&fixture.base && fixture.enabled && physical_on);sequence++;irqs++; }
static void drm_crtc_vblank_on(struct drm_crtc *c);
static void drm_crtc_vblank_off(struct drm_crtc *c);
static void drm_crtc_wait_one_vblank(struct drm_crtc *c) { assert(c==&fixture.base);if(deliver)service(); }
#if TEST_CMDQ
static void wake_up(unsigned int *q) { (*q)++; }
static void pm_runtime_mark_last_busy(struct device *d) { assert(d==&gce_dev); }
static void pm_runtime_put_autosuspend(struct device *d) { assert(d==&gce_dev && pm_refs);pm_refs--; }
static int pm_runtime_resume_and_get(struct device *d) { assert(d==&gce_dev);if(queue_pm_fail)return -EIO;pm_refs++;return 0; }
static void ddp_cmdq_cb(struct mbox_client *,void *);
static void cmdq_mbox_stop(struct mbox_chan *c) {
    assert(c==&channel);
    if(queue_pending) {
        struct cmdq_cb_data data={.sta=-ECONNABORTED,.pkt=&fixture.cmdq_handle};
        queue_pending=0;ddp_cmdq_cb(&fixture.cmdq_client.client,&data);
    }
}
static void dma_sync_single_for_cpu(struct device *d,u64 base,unsigned int len,int dir)
{ assert(d==&gce_dev && !base && len==4096 && dir==DMA_TO_DEVICE && !queue_pending); }
static void cmdq_pkt_clear_event(struct cmdq_pkt *q,u32 ev) { (void)q;(void)ev;hw(); }
static void cmdq_pkt_wfe(struct cmdq_pkt *q,u32 ev,bool clear) { (void)q;(void)ev;assert(!clear);hw(); }
static int cmdq_pkt_eoc(struct cmdq_pkt *q) { q->cmd_buf_size=8;hw();return 0; }
static void dma_sync_single_for_device(struct device *d,u64 base,unsigned int len,int dir)
{ assert(d==&gce_dev && !base && len==8 && dir==DMA_TO_DEVICE); }
static int cmdq_mbox_send(struct mbox_chan *c,struct cmdq_pkt *q)
{ assert(c==&channel && q==&fixture.cmdq_handle && !queue_pending);queue_pending++;queued++;return 0; }
#define wait_event_timeout(q,cond,time) ({ assert((time)==500);if(deliver)service();(void)(q);(cond)?1:0; })
#endif
'''

CORE_MODEL=r'''
static void drm_crtc_vblank_on(struct drm_crtc *c)
{
    assert(c==&fixture.base && fixture.enabled && physical_on);ons++;
    if(vb.inmodeset) { assert(vb.refcount>0);vb.refcount--;vb.inmodeset=false; }
    if(vb.refcount || !vb.config.offdelay_ms) {
        spin_lock(&drm.vbl_lock);assert(!drm_vblank_enable(&drm,0));spin_unlock(&drm.vbl_lock);
    }
}
static void drm_crtc_vblank_off(struct drm_crtc *c)
{
    assert(c==&fixture.base && physical_on);offs++;
    spin_lock(&drm.event_lock);spin_lock(&drm.vbl_lock);
    if(vb.enabled)mtk_crtc_disable_vblank(c);
    vb.enabled=false;if(!vb.inmodeset) { vb.refcount++;vb.inmodeset=true; }
    spin_unlock(&drm.vbl_lock);spin_unlock(&drm.event_lock);
}
'''

HELPERS=r'''
static void service(void)
{
    if(!deliver)return;
    mtk_crtc_ddp_irq(&fixture.base);
#if TEST_CMDQ
    if(queue_pending) {
        struct cmdq_cb_data data={.sta=queue_fail?-EIO:0,.pkt=&fixture.cmdq_handle};queue_pending=0;
        ddp_cmdq_cb(&fixture.cmdq_client.client,&data);
    }
#endif
}
#define for_each_oldnew_crtc_in_state(s,c,o,n,i) \
    for((i)=0,(c)=&fixture.base,(o)=(s)->old_state,(n)=(s)->new_state;(i)<1;(i)++)
#define for_each_new_crtc_in_state(s,c,n,i) \
    for((i)=0,(c)=&fixture.base,(n)=(s)->new_state;(i)<1;(i)++)
#define for_each_oldnew_plane_in_state(s,p,o,n,i) \
    for((i)=0,(p)=&fixture_plane,(o)=(s)->old_plane,(n)=(s)->new_plane;(i)<1;(i)++)
#define for_each_old_plane_in_state(s,p,o,i) \
    for((i)=0,(p)=&fixture_plane,(o)=(s)->old_plane;(i)<1;(i)++)
static bool drm_atomic_plane_disabling(struct drm_plane_state *o,struct drm_plane_state *n)
{ return o->crtc && !n->crtc; }
static bool drm_atomic_plane_enabling(struct drm_plane_state *o,struct drm_plane_state *n)
{ return !o->crtc && n->crtc; }
static bool plane_crtc_active(struct drm_plane_state *p) { return p->crtc && p->crtc->state->active; }
static bool drm_atomic_crtc_needs_modeset(struct drm_crtc_state *s) { (void)s;return transaction.modeset; }
static void plane_update(struct drm_plane *p,struct drm_atomic_state *s)
{ assert(p==&fixture_plane && s==&transaction);plane_state.pending.dirty=true;updates++; }
static void drm_atomic_helper_commit_modeset_disables(struct drm_device *d,struct drm_atomic_state *s)
{ assert(d==&drm);if(s->modeset && s->old_state->active)mtk_crtc_atomic_disable(&fixture.base,s); }
static void drm_atomic_helper_commit_modeset_enables(struct drm_device *d,struct drm_atomic_state *s)
{ assert(d==&drm);if(s->modeset && s->new_state->active)mtk_crtc_atomic_enable(&fixture.base,s); }
static void drm_atomic_helper_commit_hw_done(struct drm_atomic_state *s) { assert(s==&transaction);hw_done++; }
static void drm_atomic_helper_wait_for_vblanks(struct drm_device *d,struct drm_atomic_state *s)
{
    assert(d==&drm);if(!s->new_state->active)return;
    if(!drm_crtc_vblank_get(&fixture.base)) { service();drm_crtc_vblank_put(&fixture.base); }
}
static void drm_atomic_helper_cleanup_planes(struct drm_device *d,struct drm_atomic_state *s)
{ assert(d==&drm && s==&transaction);cleanups++; }
'''

TESTS=r'''
static void arm(unsigned int i,bool user)
{
    assert(i<8 && !events[i].base.completion && !completions[i].done);
    events[i].base.completion=&completions[i];events[i].base.completion_release=release_completion;
    events[i].base.fence=&fences[i];events[i].event.base.type=DRM_EVENT_FLIP_COMPLETE;
    if(user) { events[i].base.file_priv=&files[i];events[i].base.pending_link.linked=true; }
    current.base.event=&events[i];
}
static void done(unsigned int i)
{
    assert(completions[i].done==1 && completions[i].released==1);
    assert(fences[i].signaled==1 && fences[i].put==1 && !events[i].base.completion);
    assert(!events[i].pipe && events[i].event.vbl.tv_sec==1 && events[i].event.vbl.tv_usec==234567);
    if(events[i].base.file_priv)assert(files[i].event_wait==1 && events[i].base.link.linked);
    else assert(events[i].base.freed);
}
static void init(unsigned int path,int delay)
{
    assert(!lock_depth);(void)channel;
    memset(&fixture,0,sizeof(fixture));memset(&current,0,sizeof(current));memset(&previous,0,sizeof(previous));
    memset(events,0,sizeof(events));memset(completions,0,sizeof(completions));memset(fences,0,sizeof(fences));memset(files,0,sizeof(files));
    memset(&plane_state,0,sizeof(plane_state));memset(&old_plane,0,sizeof(old_plane));memset(&vb,0,sizeof(vb));
    warnings=hw_writes=gamma_writes=ctm_writes=config_writes=layer_writes=0;
    sent=put_refs=get_calls=ons=offs=irqs=updates=hw_done=cleanups=0;
    queued=queue_pending=pm_refs=domain_refs=main_refs=0;sequence=0;
    start_error=hw_error=enable_error=0;physical_on=false;deliver=true;queue_fail=queue_pm_fail=false;
    drm.event_lock=(struct lock){.rank=2};drm.vbl_lock=(struct lock){.rank=3};drm.vblank_time_lock=(struct lock){.rank=4};
    fixture.config_lock=(struct lock){.rank=1};
    static const struct drm_crtc_funcs funcs={.enable_vblank=mtk_crtc_enable_vblank};
    static const struct drm_crtc_helper_funcs helpers={.atomic_begin=mtk_crtc_atomic_begin,.atomic_flush=mtk_crtc_atomic_flush};
    static const struct drm_plane_helper_funcs plane_helpers={.atomic_update=plane_update};
    fixture.base=(struct drm_crtc){.dev=&drm,.state=&current.base,.funcs=&funcs,.helper_private=&helpers};
    fixture.ddp_comp=comp_ptrs;fixture.ddp_comp_nr=2;fixture.planes=&fixture_plane;fixture.layer_nr=1;
    comp[0]=(struct mtk_ddp_comp){.dev=&display_dev,.id=0};comp[1]=(struct mtk_ddp_comp){.dev=&display_dev,.id=1};
    fixture_plane=(struct drm_plane){.state=&plane_state.base,.helper_private=&plane_helpers};plane_state.base.crtc=&fixture.base;
    transaction=(struct drm_atomic_state){.dev=&drm,.old_state=&previous,.new_state=&current.base,
        .old_plane=&old_plane,.new_plane=&plane_state.base,.modeset=true};
    current.base.active=current.base.color_mgmt_changed=true;current.pending_config=true;
    soc.shadow_register=path==1;vb.config.offdelay_ms=delay;
#if TEST_CMDQ
    fixture.cmdq_client.chan=path==2?&channel:NULL;fixture.cmdq_handle.buf_size=4096;
#else
    assert(path<2);
#endif
}
static void disabled(void)
{
    assert(!fixture.enabled && !physical_on && !fixture.event && !current.base.event);
    assert(!fixture.pending_needs_vblank && !queue_pending && !pm_refs && !domain_refs && !main_refs);
    assert(vb.refcount==(int)vb.inmodeset && !warnings && !lock_depth);
}
static void stop(unsigned int id,bool user)
{
    previous.active=current.base.active;current.base.active=false;transaction.modeset=true;arm(id,user);
    drm_atomic_helper_commit_tail_rpm(&transaction);done(id);
#if TEST_CMDQ
    assert(!queue_pending && !fixture.cmdq_pending && !fixture.cmdq_page_flip);
#endif
    disabled();
}
int main(void)
{
    unsigned int cases=0;
    for(unsigned int path=0;path<(TEST_CMDQ?3U:2U);path++)
    for(int delay=-1;delay<=1;delay++)for(unsigned int user=0;user<2;user++) {
        for(unsigned int fail=0;fail<2;fail++) {
            init(path,delay);if(fail)hw_error=1;else start_error=1;
            arm(0,user);drm_atomic_helper_commit_tail_rpm(&transaction);done(0);disabled();
            assert(!hw_writes && !put_refs && !ons && !offs && updates==1 && cleanups==1 && hw_done==1);
            assert(plane_state.pending.dirty && !plane_state.pending.config);
            /* An active atomic state does not imply successful hardware startup. */
            previous.active=true;transaction.modeset=false;arm(1,!user);
            drm_atomic_helper_commit_tail_rpm(&transaction);done(1);disabled();
            assert(!hw_writes && !put_refs);
            mtk_crtc_update_config(&fixture,true);mtk_crtc_ddp_irq(&fixture.base);
            mtk_crtc_async_update(&fixture.base,&fixture_plane,&transaction);
            assert(mtk_crtc_enable_vblank(&fixture.base)==-EINVAL);
            mtk_crtc_disable_vblank(&fixture.base);assert(!hw_writes && !irqs && !fixture.pending_needs_vblank);
            stop(2,user);assert(!hw_writes && !put_refs);
            /* A later explicit modeset retries normally. */
            start_error=hw_error=0;previous.active=false;current.base.active=true;arm(3,!user);
            drm_atomic_helper_commit_tail_rpm(&transaction);done(3);
            assert(fixture.enabled && physical_on && !fixture.event && !vb.refcount);
            assert(gamma_writes==2 && ctm_writes==2 && layer_writes && config_writes && ons==1);
            stop(4,user);cases++;
        }
        /* Lost vblank/failed CMDQ completion: shutdown must release private event. */
        init(path,delay);deliver=false;arm(0,user);drm_atomic_helper_commit_tail_rpm(&transaction);
        assert(!completions[0].done && vb.refcount==1);
#if TEST_CMDQ
        assert(path==2 ? fixture.cmdq_page_flip==&events[0] : fixture.event==&events[0]);
#else
        assert(fixture.event==&events[0]);
#endif
#if TEST_CMDQ
        if(path==2) { queue_fail=true;deliver=true;service();deliver=false;assert(!pm_refs && !fixture.cmdq_page_flip && completions[0].done); }
#endif
        stop(1,!user);done(0);assert(sent==2 && put_refs>=1);cases++;
        /* vblank acquisition can fail even after hardware startup. */
        init(path,1);mtk_crtc_atomic_enable(&fixture.base,&transaction);enable_error=1;
        arm(0,user);mtk_crtc_atomic_begin(&fixture.base,&transaction);done(0);
        assert(!fixture.event && !vb.refcount && !put_refs);
        enable_error=0;deliver=true;stop(1,!user);cases++;
        /* Retire an old pending event without overwriting its reference. */
        init(path,delay);deliver=false;arm(0,user);drm_atomic_helper_commit_tail_rpm(&transaction);
#if TEST_CMDQ
        if(path==2) { queue_fail=true;deliver=true;service();deliver=false;queue_fail=false; }
#endif
        arm(1,!user);mtk_crtc_atomic_begin(&fixture.base,&transaction);done(0);
        assert(fixture.event==&events[1] && !fixture.pending_needs_vblank && vb.refcount==1);
        /* Old pending flag must not complete the newly queued event before flush. */
        mtk_drm_finish_page_flip(&fixture);assert(!completions[1].done);
        mtk_crtc_atomic_flush(&fixture.base,&transaction);deliver=true;service();done(1);
        assert(!fixture.event && !vb.refcount);stop(2,user);cases++;
    }
    printf("PASS: %u commit/event scenarios with CMDQ compiled %s; real DRM vblank get/put and completion delivery\n",cases,TEST_CMDQ?"in":"out");
    puts("Core on/off, hardware, scheduling, other components and mailbox transport are modeled.");
    return 0;
}
'''


def build(source, cmdq):
    s=source.read_text();vblank=(SRC/'drm_vblank.c').read_text();helper=(SRC/'drm_atomic_helper.c').read_text()
    code=TYPES+command.block(s,'mtk_crtc')+command.block(s,'mtk_crtc_state')+MODEL
    core=[('drm_file.c','drm_send_event_helper'),('drm_file.c','drm_send_event_timestamp_locked')]
    for file,name in core:code+=command.block((SRC/file).read_text(),name)
    for name in ('send_vblank_event','drm_crtc_send_vblank_event','__enable_vblank',
                 'drm_vblank_enable','drm_vblank_get','drm_crtc_vblank_get','drm_vblank_put','drm_crtc_vblank_put'):
        body=command.block(vblank,name)
        if name=='drm_crtc_vblank_get':body=body.replace('{\n','{\n\tget_calls++;\n',1)
        if name=='drm_crtc_vblank_put':body=body.replace('{\n','{\n\tput_refs++;\n',1)
        if name=='__enable_vblank':body=body.replace('{\n','{\n\tif(enable_error)return -EIO;\n',1)
        code+=body
    code+=CORE_MODEL
    for name in ('to_mtk_crtc','to_mtk_crtc_state','mtk_crtc_finish_page_flip','mtk_drm_finish_page_flip',
                 'ddp_cmdq_cb','mtk_crtc_cmdq_clear_pending','mtk_crtc_ddp_hw_fini','mtk_crtc_ddp_config','mtk_crtc_update_config',
                 'mtk_crtc_ddp_irq','mtk_crtc_enable_vblank','mtk_crtc_disable_vblank','mtk_crtc_async_update',
                 'mtk_crtc_atomic_enable','mtk_crtc_atomic_disable','mtk_crtc_atomic_begin','mtk_crtc_atomic_flush'):
        body=command.block(s,name)
        code+=('#if TEST_CMDQ\n'+body+'#endif\n') if name in ('ddp_cmdq_cb','mtk_crtc_cmdq_clear_pending') else body
    code+=HELPERS
    for name in ('drm_atomic_helper_commit_planes','drm_atomic_helper_fake_vblank','drm_atomic_helper_commit_tail_rpm'):
        code+=command.block(helper,name)
    code+=TESTS
    path=ROOT/'out'/f'crtc-events-{cmdq}.c';path.write_text(code);binary=path.with_suffix('')
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-sign-compare',
        '-Wno-unused-parameter','-Wno-unused-but-set-variable',f'-DTEST_CMDQ={cmdq}',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',str(path),'-o',str(binary)],check=True)
    return binary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SRC/'mediatek/mtk_crtc.c')
    args=parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT):raise SystemExit('Source must stay under /rabbitr1')
    for cmdq in (0,1):subprocess.run([str(build(args.source,cmdq))],check=True)


if __name__=='__main__':main()
