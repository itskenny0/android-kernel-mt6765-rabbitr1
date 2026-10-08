#!/usr/bin/env python3
"""Race the production GCE IRQ against system suspend using pthreads.

The controller callbacks and force-suspend/resume helpers execute unchanged.
MMIO, clocks, PM bookkeeping and IRQ-core synchronization are host models.
This checks software ordering, not physical clock or DMA behavior.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
spec = importlib.util.spec_from_file_location('flush', Path(__file__).with_name('test-cmdq-flush.py'))
flush = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flush)
block = flush.block

TYPES = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
typedef int irqreturn_t;
#define __iomem
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define READ_ONCE(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#define WRITE_ONCE(x,v) __atomic_store_n(&(x),(v),__ATOMIC_RELAXED)
#define smp_load_acquire(p) __atomic_load_n((p),__ATOMIC_ACQUIRE)
#define smp_store_release(p,v) __atomic_store_n((p),(v),__ATOMIC_RELEASE)
#define smp_mb() atomic_thread_fence(memory_order_seq_cst)
#define IS_ENABLED(x) TEST_PM
#define IRQ_NONE 0
#define IRQ_HANDLED 1
#define EXPORT_SYMBOL_GPL(x)
#define dev_err(dev,...) ((void)(dev))
#define dev_err_ratelimited(dev,...) ((void)(dev))
#define CMDQ_INST_SIZE 8
#define CMDQ_OP_CODE_SHIFT 24
#define for_each_clear_bit(bit,addr,count) for(bit=0;bit<(int)(count);bit++)if(!(*(addr)&BIT(bit)))
struct device { void *data;struct { bool needs_force_resume,smart_suspend; } power; };
struct clk_bulk_data;
struct mbox_client;
struct mbox_chan;
struct mbox_controller { struct device *dev;struct mbox_chan *chans; };
typedef pthread_mutex_t spinlock_t;
struct mbox_chan { spinlock_t lock; };
static void *dev_get_drvdata(struct device *d) { return d->data; }
static _Thread_local unsigned int locks_held;
static _Thread_local bool in_irq;
static void checkpoint(unsigned int point);
static void channel_unlock(spinlock_t *lock);
#define spin_lock_irqsave(l,f) do { (f)=0;assert(!pthread_mutex_lock(l));locks_held++; } while(0)
#define spin_unlock_irqrestore(l,f) do { (void)(f);channel_unlock(l); } while(0)
#define readl_poll_timeout_atomic(addr,val,cond,delay,timeout) ({ \
    int ret=-ETIMEDOUT;for(int n=0;n<4;n++){val=readl(addr);if(cond){ret=0;break;}}ret; })
static void kfree(void *p) { (void)p;assert(!"unexpected task retirement"); }
static void mbox_chan_received_data(struct mbox_chan *ch,void *p) {
    (void)ch;(void)p;assert(!"unexpected callback");
}
'''

MODEL = r'''
static struct device device;
static struct cmdq controller;
static struct gce_plat platform;
static struct cmdq_thread threads[2];
static struct mbox_chan channels[2];
static u32 regs[0x1000/4];
static pthread_mutex_t pm_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t schedule_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t schedule_cond=PTHREAD_COND_INITIALIZER;
static atomic_bool clock_on;
static atomic_uint reads,writes,pm_gets,pm_puts,syncs;
static bool pm_enabled,pm_active,fail_suspend,fail_enable;
static unsigned int users;
static bool irq_running,paused,release_irq,sync_waiting,pm_done;
static int pause_at=-1,suspend_result;
static unsigned int late_irq_accesses;
static int cmdq_runtime_suspend(struct device *dev);
static int cmdq_runtime_resume(struct device *dev);
static irqreturn_t cmdq_irq_handler(int irq,void *dev);

static void checkpoint(unsigned int point) {
    if(!in_irq || pause_at!=(int)point)return;
    assert(!locks_held);
    assert(!pthread_mutex_lock(&schedule_lock));
    paused=true;assert(!pthread_cond_broadcast(&schedule_cond));
    while(!release_irq)assert(!pthread_cond_wait(&schedule_cond,&schedule_lock));
    assert(!pthread_mutex_unlock(&schedule_lock));
}
static void channel_unlock(spinlock_t *lock) {
    assert(locks_held);locks_held--;assert(!pthread_mutex_unlock(lock));
    if(lock==&channels[0].lock)checkpoint(3);
}
static u32 readl(void *address) {
    if(address==(char *)regs+CMDQ_CURR_IRQ_STATUS)checkpoint(1);
    assert(atomic_load(&clock_on) && "IRQ read after clocks were disabled");
    atomic_fetch_add(&reads,1);u32 value=*(u32 *)address;
    if(address==(char *)regs+CMDQ_CURR_IRQ_STATUS)checkpoint(2);
    return value;
}
static void writel(u32 value,void *address) {
    assert(atomic_load(&clock_on) && "IRQ write after clocks were disabled");
    atomic_fetch_add(&writes,1);*(u32 *)address=value;
}
static int clk_bulk_enable(unsigned int n,struct clk_bulk_data *clocks) {
    (void)clocks;assert(n==1 && !atomic_load(&clock_on));
    if(fail_enable)return -EIO;
    atomic_store(&clock_on,true);return 0;
}
static void clk_bulk_disable(unsigned int n,struct clk_bulk_data *clocks) {
    (void)clocks;assert(n==1 && atomic_load(&clock_on));atomic_store(&clock_on,false);
}
static int pm_runtime_get_if_active(struct device *dev) {
    assert(dev==&device);assert(!pthread_mutex_lock(&pm_lock));
    int ret=pm_enabled && pm_active;
    if(ret){users++;atomic_fetch_add(&pm_gets,1);}
    assert(!pthread_mutex_unlock(&pm_lock));
    if(ret)checkpoint(0);
    return ret;
}
static void pm_runtime_put_autosuspend(struct device *dev) {
    assert(dev==&device);assert(!pthread_mutex_lock(&pm_lock));
    assert(users);users--;atomic_fetch_add(&pm_puts,1);
    assert(!pthread_mutex_unlock(&pm_lock));
}
static void pm_runtime_mark_last_busy(struct device *dev) {
    assert(dev==&device);checkpoint(4);
}
static void pm_runtime_disable(struct device *dev) {
    assert(dev==&device);assert(!pthread_mutex_lock(&pm_lock));
    assert(pm_enabled);pm_enabled=false;assert(!pthread_mutex_unlock(&pm_lock));
}
static void pm_runtime_enable(struct device *dev) {
    assert(dev==&device);assert(!pthread_mutex_lock(&pm_lock));
    assert(!pm_enabled);pm_enabled=true;assert(!pthread_mutex_unlock(&pm_lock));
}
static bool pm_runtime_status_suspended(struct device *dev) { assert(dev==&device);return !pm_active; }
static void pm_runtime_set_suspended(struct device *dev) { assert(dev==&device);pm_active=false; }
static bool pm_runtime_need_not_resume(struct device *dev) {
    assert(dev==&device);assert(!pthread_mutex_lock(&pm_lock));
    bool ret=users<=1;assert(!pthread_mutex_unlock(&pm_lock));return ret;
}
static bool dev_pm_smart_suspend(struct device *dev) { return dev->power.smart_suspend; }
#define dev_pm_enable_wake_irq_check(d,b) ((void)(d))
#define dev_pm_disable_wake_irq_check(d,b) ((void)(d))
#define dev_pm_enable_wake_irq_complete(d) ((void)(d))
static int force_runtime_suspend(struct device *dev) {
    if(fail_suspend)return -EBUSY;
    return cmdq_runtime_suspend(dev);
}
static int force_runtime_resume(struct device *dev) { return cmdq_runtime_resume(dev); }
#define GET_CALLBACK(dev,callback) force_##callback
static void synchronize_irq(unsigned int irq) {
    assert(irq==42 && !locks_held);atomic_fetch_add(&syncs,1);
    assert(!pthread_mutex_lock(&schedule_lock));
    sync_waiting=true;assert(!pthread_cond_broadcast(&schedule_cond));
    while(irq_running)assert(!pthread_cond_wait(&schedule_cond,&schedule_lock));
    assert(!pthread_mutex_unlock(&schedule_lock));
    /* A shared line can call us again immediately after the barrier. */
    unsigned int before=atomic_load(&reads)+atomic_load(&writes)+atomic_load(&pm_gets);
    (void)cmdq_irq_handler(42,&controller);
    late_irq_accesses+=atomic_load(&reads)+atomic_load(&writes)+atomic_load(&pm_gets)-before;
}
'''

TESTS = r'''
static void setup(bool active,unsigned int references) {
    memset(&device,0,sizeof(device));memset(&controller,0,sizeof(controller));
    memset(&platform,0,sizeof(platform));memset(threads,0,sizeof(threads));memset(regs,0,sizeof(regs));
    platform.thread_nr=2;platform.gce_num=1;controller.pdata=&platform;controller.base=regs;
    controller.irq=42;controller.irq_mask=3;controller.mbox.dev=&device;controller.mbox.chans=channels;
    controller.thread=threads;controller.irq_ready=true;controller.clocks_enabled=active;device.data=&controller;
    for(unsigned int i=0;i<2;i++){
        threads[i].base=(char *)regs+CMDQ_THR_BASE+i*CMDQ_THR_SIZE;threads[i].chan=&channels[i];
        INIT_LIST_HEAD(&threads[i].task_busy_list);
    }
    atomic_store(&clock_on,active);pm_active=active;pm_enabled=true;users=references;
    atomic_store(&reads,0);atomic_store(&writes,0);atomic_store(&pm_gets,0);atomic_store(&pm_puts,0);atomic_store(&syncs,0);
    fail_suspend=fail_enable=irq_running=paused=release_irq=sync_waiting=pm_done=false;
    pause_at=-1;late_irq_accesses=0;
}
static void *run_irq(void *arg) {
    (void)arg;in_irq=true;(void)cmdq_irq_handler(42,&controller);in_irq=false;
    assert(!pthread_mutex_lock(&schedule_lock));irq_running=false;
    assert(!pthread_cond_broadcast(&schedule_cond));assert(!pthread_mutex_unlock(&schedule_lock));return NULL;
}
static void *run_suspend(void *arg) {
    (void)arg;suspend_result=cmdq_suspend(&device);
    assert(!pthread_mutex_lock(&schedule_lock));pm_done=true;
    assert(!pthread_cond_broadcast(&schedule_cond));assert(!pthread_mutex_unlock(&schedule_lock));return NULL;
}
static void no_irq_access(void) {
    unsigned int before=atomic_load(&reads)+atomic_load(&writes)+atomic_load(&pm_gets);
    assert(cmdq_irq_handler(42,&controller)==IRQ_NONE);
    assert(before==atomic_load(&reads)+atomic_load(&writes)+atomic_load(&pm_gets));
}
static void raced_suspend(unsigned int point,unsigned int references) {
    pthread_t irq_worker,pm_worker;setup(true,references);pause_at=point;irq_running=true;
    assert(!pthread_create(&irq_worker,NULL,run_irq,NULL));
    assert(!pthread_mutex_lock(&schedule_lock));
    while(!paused)assert(!pthread_cond_wait(&schedule_cond,&schedule_lock));
    assert(!pthread_mutex_unlock(&schedule_lock));
    assert(!pthread_create(&pm_worker,NULL,run_suspend,NULL));
    assert(!pthread_mutex_lock(&schedule_lock));
    while(!sync_waiting && !pm_done)assert(!pthread_cond_wait(&schedule_cond,&schedule_lock));
    release_irq=true;assert(!pthread_cond_broadcast(&schedule_cond));
    assert(!pthread_mutex_unlock(&schedule_lock));
    assert(!pthread_join(irq_worker,NULL));assert(!pthread_join(pm_worker,NULL));
    assert(!suspend_result && !atomic_load(&clock_on) && !controller.irq_ready);
    assert(atomic_load(&syncs)==1 && !late_irq_accesses && users==references);
    assert(atomic_load(&pm_gets)==atomic_load(&pm_puts));no_irq_access();
    assert(!cmdq_resume(&device) && !controller.suspended && controller.irq_ready && pm_enabled);
    if(references<=1){assert(!atomic_load(&clock_on));no_irq_access();}
    else { assert(atomic_load(&clock_on));assert(cmdq_irq_handler(42,&controller)==IRQ_HANDLED); }
}
int main(void) {
    unsigned int cases=0;
    for(unsigned int i=0;i<2;i++)assert(!pthread_mutex_init(&channels[i].lock,NULL));
    if(TEST_PM){
        for(unsigned int point=0;point<5;point++)for(unsigned int refs=1;refs<=2;refs++){
            raced_suspend(point,refs);cases++;
        }
        setup(false,1);assert(!cmdq_suspend(&device));no_irq_access();
        assert(!cmdq_resume(&device) && controller.irq_ready && !controller.suspended);no_irq_access();cases++;
        setup(true,2);fail_suspend=true;
        assert(cmdq_suspend(&device)==-EBUSY && controller.irq_ready && !controller.suspended && pm_enabled);
        assert(atomic_load(&clock_on) && atomic_load(&syncs)==1 && !late_irq_accesses);
        assert(cmdq_irq_handler(42,&controller)==IRQ_HANDLED);cases++;
        setup(true,2);assert(!cmdq_suspend(&device));fail_enable=true;
        assert(cmdq_resume(&device)==-EIO && !controller.irq_ready && controller.suspended);
        no_irq_access();assert(!atomic_load(&clock_on));cases++;
        for(unsigned int i=0;i<2;i++){
            setup(true,2);struct list_head pending;INIT_LIST_HEAD(&pending);
            list_move_tail(&pending,&threads[i].task_busy_list);
            assert(cmdq_suspend(&device)==-EBUSY && !controller.suspended && controller.irq_ready);
            assert(atomic_load(&clock_on) && !atomic_load(&syncs) && pm_enabled);
            list_del(&pending);cases++;
        }
        setup(true,0);controller.irq_ready=false;no_irq_access();cases++;
    }else{
        /* CONFIG_PM=n: probe keeps clocks on; IRQ must not use PM stubs. */
        setup(true,0);pm_enabled=false;
        assert(cmdq_irq_handler(42,&controller)==IRQ_HANDLED && !atomic_load(&pm_gets) && !atomic_load(&pm_puts));cases++;
        controller.irq_ready=false;atomic_store(&clock_on,false);no_irq_access();cases++;
    }
    for(unsigned int i=0;i<2;i++)assert(!pthread_mutex_destroy(&channels[i].lock));
    printf("PASS: %u GCE IRQ/power cases (CONFIG_PM=%d); concurrent handlers, forced suspend and resume failures\n",cases,TEST_PM);
}
'''


def build(source_path, pm=1):
    source = source_path.read_text()
    core = (SRC/'drivers/base/power/runtime.c').read_text()
    header = (SRC/'include/linux/mailbox/mtk-cmdq-mailbox.h').read_text()
    text = TYPES
    text += flush.TYPES[flush.TYPES.index('struct list_head'):flush.TYPES.index('typedef struct { bool held; } spinlock_t;')]
    text += source[source.index('#define CMDQ_MBOX_AUTOSUSPEND_DELAY_MS'):source.index('struct cmdq_thread')]
    text += ''.join(block(header, n) for n in ('cmdq_cb_data', 'cmdq_mbox_priv', 'cmdq_pkt'))
    text += ''.join(block(source, n) for n in ('cmdq_thread', 'cmdq_task', 'cmdq', 'gce_plat'))
    text += MODEL
    text += ''.join(block(core, n) for n in ('pm_runtime_force_suspend', 'pm_runtime_force_resume'))
    text += ''.join(block(source, n) for n in ('cmdq_convert_gce_addr', 'cmdq_revert_gce_addr',
        'cmdq_thread_suspend', 'cmdq_thread_resume', 'cmdq_thread_reset', 'cmdq_thread_disable',
        'cmdq_task_exec_done', 'cmdq_thread_irq_handler', 'cmdq_irq_handler', 'cmdq_gctl_value_toggle',
        'cmdq_runtime_resume', 'cmdq_runtime_suspend', 'cmdq_suspend', 'cmdq_resume'))
    text += TESTS
    out = ROOT/'out/cmdq-power-tests'
    out.mkdir(exist_ok=True)
    generated = out/f'power-pm{pm}.c'
    generated.write_text(text)
    binary = generated.with_suffix('')
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
        '-Wno-unused-parameter', '-Wno-sign-compare', f'-DTEST_PM={pm}', '-pthread',
        '-fsanitize=address,undefined', '-fno-pie', '-no-pie', '-O1', '-g', str(generated),
        '-o', str(binary)], check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'drivers/mailbox/mtk-cmdq-mailbox.c')
    args = parser.parse_args()
    for pm in (1, 0):
        subprocess.run([str(build(args.source, pm))], check=True, timeout=20)


if __name__ == '__main__':
    main()
