#!/usr/bin/env python3
"""Exercise GCE flush ownership with production controller and mailbox helpers.

MMIO, runtime PM, scheduling, allocation and locks are models. This does not
prove physical DMA quiescence or fix client/shutdown lifetime bugs.
"""
import argparse
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')


def block(source, name):
    match = re.search(r'^[a-zA-Z_][^\n]*\b'+re.escape(name)+r'(?:\(|\s*\{)', source, re.M)
    assert match, name
    end = source.index('\n}', match.start())+2
    if source[end:end+1] == ';':
        end += 1
    return source[match.start():end]+'\n'


def macro(source, name):
    match = re.search(r'^#define '+name+r'\(', source, re.M)
    assert match, name
    lines = source[match.start():].splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.rstrip().endswith('\\'):
            return ''.join(lines[:index+1])
    raise AssertionError(name)


TYPES = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t s64;
typedef uint64_t dma_addr_t;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define unlikely(x) (x)
#define WARN_ON(x) ({ bool bad=(x); warnings+=bad; bad; })
#define dev_err(dev,...) ((void)(dev))
#define GFP_ATOMIC 0
#define ENOTSUPP 524
#define DMA_TO_DEVICE 1
#define NSEC_PER_USEC 1000
#define USEC_PER_MSEC 1000
#define MBOX_NO_MSG ((void *)-1)
#define MBOX_TX_QUEUE_LEN 20
#define MBOX_TXDONE_BY_POLL BIT(1)
#define MBOX_TXDONE_BY_ACK BIT(2)
#define HRTIMER_MODE_REL 0
#define msecs_to_jiffies(x) (x)
#define barrier() __asm__ __volatile__("" ::: "memory")
#define smp_mb() barrier()
#define cpu_relax() barrier()
#define CMDQ_INST_SIZE 8
#define CMDQ_OP_CODE_SHIFT 24
struct list_head { struct list_head *next,*prev; };
static void INIT_LIST_HEAD(struct list_head *h) { h->next=h->prev=h; }
static bool list_empty(const struct list_head *h) { return h->next==h; }
static void list_del(struct list_head *n) { n->prev->next=n->next; n->next->prev=n->prev; }
static void list_move_tail(struct list_head *n,struct list_head *h) { list_del(n);n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n; }
#define list_entry(p,t,m) container_of(p,t,m)
#define list_first_entry_or_null(h,t,m) (list_empty(h)?NULL:list_entry((h)->next,t,m))
#define list_last_entry(h,t,m) list_entry((h)->prev,t,m)
#define list_for_each_entry_safe(p,n,h,m) \
    for (p=list_entry((h)->next,typeof(*p),m),n=list_entry(p->m.next,typeof(*p),m); \
         &p->m!=(h); p=n,n=list_entry(n->m.next,typeof(*n),m))
typedef struct { bool held; } spinlock_t;
static unsigned int lock_count,warnings;
static void lock(spinlock_t *l) { assert(!l->held && !lock_count);l->held=true;lock_count++; }
static void unlock(spinlock_t *l) { assert(l->held && lock_count==1);l->held=false;lock_count--; }
#define spin_lock_irqsave(l,f) do { (f)=0;lock(l); } while(0)
#define spin_unlock_irqrestore(l,f) do { (void)(f);unlock(l); } while(0)
struct auto_lock { spinlock_t *p;bool once; };
static struct auto_lock acquire(spinlock_t *l) { lock(l);return (struct auto_lock){l,true}; }
static void release(struct auto_lock *g) { unlock(g->p); }
#define guard(kind) struct auto_lock __attribute__((cleanup(release))) _guard=acquire
#define scoped_guard(kind,p) for (bool _once=true;_once;_once=false) \
    for (struct auto_lock __attribute__((cleanup(release))) _guard=acquire(p);_once;_once=false)
struct device { void *data;int refs;bool on,fail; };
struct completion { unsigned int count; };
struct mbox_client { bool tx_block;unsigned int tx_tout;void (*tx_prepare)(struct mbox_client *,void *);void (*tx_done)(struct mbox_client *,void *,int);void (*rx_callback)(struct mbox_client *,void *); };
struct mbox_chan;
struct mbox_chan_ops { int (*send_data)(struct mbox_chan *,void *);int (*flush)(struct mbox_chan *,unsigned long); };
struct mbox_controller { struct device *dev;const struct mbox_chan_ops *ops;spinlock_t poll_hrt_lock;int poll_hrt; };
struct mbox_chan { struct mbox_controller *mbox;unsigned int txdone_method;struct mbox_client *cl;struct completion tx_complete;void *active_req;unsigned int msg_count,msg_free;void *msg_data[MBOX_TX_QUEUE_LEN];spinlock_t lock;void *con_priv; };
struct clk_bulk_data;
static void *dev_get_drvdata(struct device *d) { return d->data; }
static void complete(struct completion *c) { c->count++; }
static int wait_for_completion_timeout(struct completion *c,unsigned long t) { (void)c;(void)t;assert(false);return 0; }
static void hrtimer_start(int *t,int v,int m) { (void)t;(void)v;(void)m;assert(false); }
'''

MODEL = r'''
static struct device device;
static struct cmdq controller;
static struct cmdq_thread thread;
static struct gce_plat platform;
static struct mbox_client client;
static struct mbox_chan channel;
static u32 regs[0x1000/4];
static struct cmdq_pkt *packets[4];
static unsigned int received[4],get_calls,failed_gets,put_calls,mmio_reads,mmio_writes,allocations,callbacks;
static int results[4];
static bool fail_suspend,fail_reset,fail_alloc;
static u64 elapsed_us,complete_at;
static bool scheduled;
static void deliver_irq(void);
static int pm_runtime_get_sync(struct device *d) { get_calls++;d->refs++;if(d->fail){failed_gets++;return -EIO;}d->on=true;return 0; }
static int pm_runtime_resume_and_get(struct device *d) { get_calls++;if(d->fail){failed_gets++;return -EIO;}d->refs++;d->on=true;return 0; }
static void pm_runtime_mark_last_busy(struct device *d) { assert(d->on && d->refs>0); }
static void pm_runtime_put_autosuspend(struct device *d) { assert(d->refs>0);put_calls++;if(!(--d->refs))d->on=false; }
static void *allocate(size_t size) { if(fail_alloc)return NULL;void *p=calloc(1,size);assert(p);allocations++;return p; }
#define kzalloc_obj(obj,flags) allocate(sizeof(obj))
static void kfree(void *p) { assert(allocations);allocations--;free(p); }
static u32 readl(void *address) {
    assert(device.on && device.refs>0);
    if(scheduled && elapsed_us>=complete_at && !channel.lock.held) {
        scheduled=false;deliver_irq();
    }
    size_t offset=(char *)address-(char *)regs;
    assert(offset<sizeof(regs) && !(offset&3));mmio_reads++;
    return *(u32 *)address;
}
static void writel(u32 value,void *address) {
    assert(device.on && device.refs>0 && channel.lock.held);
    size_t offset=(char *)address-(char *)regs;
    assert(offset<sizeof(regs) && !(offset&3));mmio_writes++;
    if(offset==CMDQ_THR_BASE+CMDQ_THR_IRQ_STATUS) { *(u32 *)address&=value;return; }
    *(u32 *)address=value;
    if(offset==CMDQ_THR_BASE+CMDQ_THR_SUSPEND_TASK) {
        if(value && !fail_suspend)regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_STATUS)/4]|=CMDQ_THR_STATUS_SUSPENDED;
        if(!value)regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_STATUS)/4]&=~CMDQ_THR_STATUS_SUSPENDED;
    }
    if(offset==CMDQ_THR_BASE+CMDQ_THR_WARM_RESET && value && !fail_reset)
        memset((char *)regs+CMDQ_THR_BASE,0,CMDQ_THR_SIZE);
}
static void udelay(unsigned long us) { elapsed_us+=us; }
static void sync_dma(struct device *dev,dma_addr_t pa,size_t size,int dir) {
    assert(dev==&device && dir==DMA_TO_DEVICE && device.on && channel.lock.held);
    bool found=false;
    for(unsigned int i=0;i<4;i++)if(packets[i] && packets[i]->pa_base==pa) {
        assert(size<=packets[i]->buf_size);found=true;
    }
    assert(found);
}
#define dma_sync_single_for_cpu sync_dma
#define dma_sync_single_for_device sync_dma
static void receive(struct mbox_client *cl,void *message) {
    struct cmdq_cb_data *data=message;
    assert(cl==&client && channel.lock.held && device.on);
    if(data->sta==-ECONNABORTED) {
        /* A callback may free DMA memory immediately, not after flush returns. */
        assert(!regs[(CMDQ_THR_BASE+CMDQ_THR_ENABLE_TASK)/4]);
        assert(!regs[(CMDQ_THR_BASE+CMDQ_THR_WARM_RESET)/4]);
    }
    unsigned int i;
    for(i=0;i<4;i++)if(packets[i]==data->pkt)break;
    assert(i<4 && received[i]==0);received[i]++;results[i]=data->sta;callbacks++;
    free(packets[i]->va_base);free(packets[i]);packets[i]=NULL;
    pm_runtime_mark_last_busy(&device);pm_runtime_put_autosuspend(&device);
}
'''

TESTS = r'''
static const struct mbox_chan_ops ops={.send_data=cmdq_mbox_send_data,.flush=cmdq_mbox_flush};
static unsigned int count_tasks(void) {
    unsigned int n=0;struct list_head *h=&thread.task_busy_list;
    for(struct list_head *p=h->next;p!=h;p=p->next)n++;
    return n;
}
static void deliver_irq(void) {
    regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_ADDR)/4]=regs[(CMDQ_THR_BASE+CMDQ_THR_END_ADDR)/4];
    regs[(CMDQ_THR_BASE+CMDQ_THR_IRQ_STATUS)/4]=CMDQ_THR_IRQ_DONE;
    lock(&channel.lock);cmdq_thread_irq_handler(&controller,&thread);unlock(&channel.lock);
}
static void setup(unsigned int plat) {
    assert(!allocations && !lock_count);
    for(unsigned int i=0;i<4;i++)assert(!packets[i]);
    memset(&device,0,sizeof(device));memset(&controller,0,sizeof(controller));
    memset(&thread,0,sizeof(thread));memset(&platform,0,sizeof(platform));
    memset(&client,0,sizeof(client));memset(&channel,0,sizeof(channel));
    memset(regs,0,sizeof(regs));memset(received,0,sizeof(received));memset(results,0,sizeof(results));
    callbacks=get_calls=failed_gets=put_calls=mmio_reads=mmio_writes=warnings=0;
    fail_suspend=fail_reset=fail_alloc=scheduled=false;elapsed_us=complete_at=0;
    platform.shift=plat?3:0;platform.mminfra_offset=plat==2?0x100000000ULL:0;
    controller.pdata=&platform;controller.base=regs;controller.mbox.dev=&device;controller.mbox.ops=&ops;
    device.data=&controller;thread.base=(char *)regs+CMDQ_THR_BASE;thread.chan=&channel;
    INIT_LIST_HEAD(&thread.task_busy_list);client.rx_callback=receive;
    channel.mbox=&controller.mbox;channel.con_priv=&thread;channel.cl=&client;
    channel.txdone_method=MBOX_TXDONE_BY_ACK;channel.active_req=MBOX_NO_MSG;
}
static void send_packet(unsigned int i) {
    assert(i<4 && !packets[i]);
    struct cmdq_pkt *p=calloc(1,sizeof(*p));assert(p);packets[i]=p;
    p->pa_base=0x40000000+0x1000*i;p->buf_size=p->cmd_buf_size=32;
    p->va_base=calloc(1,p->buf_size);assert(p->va_base);
    assert(!pm_runtime_resume_and_get(&device));
    assert(mbox_send_message(&channel,p)>=0);mbox_client_txdone(&channel,0);
}
static void populate(unsigned int count) {
    for(unsigned int i=0;i<count;i++)send_packet(i);
    assert(count_tasks()==count && device.refs==(int)count && !channel.msg_count);
    regs[(CMDQ_THR_BASE+CMDQ_THR_CURR_ADDR)/4]=cmdq_convert_gce_addr(packets[0]->pa_base+8,&platform);
}
static void finish_all(unsigned int n) {
    fail_suspend=fail_reset=false;device.fail=false;
    regs[(CMDQ_THR_BASE+CMDQ_THR_WAIT_TOKEN)/4]=CMDQ_THR_IS_WAITING;
    assert(!mbox_flush(&channel,2));
    assert(!device.refs && !device.on && !count_tasks() && !allocations && !lock_count);
    assert(callbacks==n && get_calls==put_calls+failed_gets);
    for(unsigned int i=0;i<n;i++)assert(received[i]==1 && !packets[i] && results[i]==-ECONNABORTED);
}
static void failed_flush(unsigned int n,bool suspend,bool waiting,bool reset) {
    populate(n);fail_suspend=suspend;fail_reset=reset;
    regs[(CMDQ_THR_BASE+CMDQ_THR_WAIT_TOKEN)/4]=waiting?CMDQ_THR_IS_WAITING:0;
    u64 snapshots[4][4];
    for(unsigned int i=0;i<n;i++)memcpy(snapshots[i],packets[i]->va_base,32);
    assert(mbox_flush(&channel,2)==-EFAULT);
    assert(!callbacks && count_tasks()==n && device.refs==(int)n && !channel.lock.held);
    if(suspend)assert(!regs[(CMDQ_THR_BASE+CMDQ_THR_SUSPEND_TASK)/4]);
    for(unsigned int i=0;i<n;i++)assert(!memcmp(snapshots[i],packets[i]->va_base,32));
    finish_all(n);
}
static void queued_core_probe(void) {
    /* Regression evidence for the NEXT client fix: a token is only queue admission. */
    setup(0);fail_alloc=true;send_packet(0);
    assert(channel.msg_count==1 && !count_tasks() && !callbacks);
    assert(!mbox_flush(&channel,2));
    assert(channel.msg_count==1 && !count_tasks() && !callbacks);
    fail_alloc=false;device.fail=true;
    assert(mbox_flush(&channel,2)==-EIO);
    /* Core tx_tick runs msg_submit on flush failure; now hardware owns the packet. */
    assert(!channel.msg_count && count_tasks()==1 && !callbacks);
    finish_all(1);
}
int main(void) {
    unsigned int cases=0;
    for(unsigned int plat=0;plat<3;plat++) {
        setup(plat);assert(!mbox_flush(&channel,2));
        assert(!device.refs && !device.on && !mmio_reads && !mmio_writes);cases++;
        setup(plat);device.fail=true;assert(mbox_flush(&channel,2)==-EIO);
        assert(!device.refs && !device.on && !mmio_reads && !mmio_writes);cases++;
        for(unsigned int n=1;n<=3;n++) {
            setup(plat);populate(n);finish_all(n);cases++;
            setup(plat);failed_flush(n,true,true,false);cases++;
            setup(plat);failed_flush(n,true,false,false);cases++;
            setup(plat);failed_flush(n,false,true,true);cases++;
            setup(plat);populate(n);device.fail=true;
            unsigned int reads=mmio_reads,writes=mmio_writes;
            assert(mbox_flush(&channel,2)==-EIO);
            assert(device.refs==(int)n && !callbacks && count_tasks()==n);
            assert(mmio_reads==reads && mmio_writes==writes);finish_all(n);cases++;
            setup(plat);populate(n);assert(mbox_flush(&channel,2)==-ETIMEDOUT);
            /* The kernel polling macro also accounts for one ns per iteration. */
            assert(elapsed_us>=1990 && elapsed_us<2010 && device.refs==(int)n && !callbacks);
            assert(count_tasks()==n);finish_all(n);cases++;
            /* Real IRQ handler completes work during the unlocked poll, after 1.5 ms. */
            setup(plat);populate(n);complete_at=1500;scheduled=true;
            assert(!mbox_flush(&channel,2));
            assert(elapsed_us==1500 && !scheduled && callbacks==n && !device.refs && !allocations);
            for(unsigned int i=0;i<n;i++)assert(received[i]==1 && results[i]==0);
            assert(get_calls==put_calls);cases++;
            setup(plat);populate(n);complete_at=1;scheduled=true;
            assert(!mbox_flush(&channel,0));assert(callbacks==n && !device.refs && !allocations);cases++;
        }
    }
    queued_core_probe();cases++;
    printf("PASS: %u GCE flush scenarios; callback-time frees, failed stops, retries, power balance and millisecond deadlines\n",cases);
    puts("CONFIRMED: core queue admission can precede controller acceptance; a failed flush can submit queued work");
}
'''


def build(source_path):
    source = source_path.read_text()
    core = (SRC/'drivers/mailbox/mailbox.c').read_text()
    header = (SRC/'include/linux/mailbox/mtk-cmdq-mailbox.h').read_text()
    poll = (SRC/'include/linux/iopoll.h').read_text()
    text = TYPES
    text += source[source.index('#define CMDQ_MBOX_AUTOSUSPEND_DELAY_MS'):source.index('struct cmdq_thread')]
    text += ''.join(block(header, n) for n in ('cmdq_cb_data', 'cmdq_mbox_priv', 'cmdq_pkt'))
    text += ''.join(block(source, n) for n in ('cmdq_thread', 'cmdq_task', 'cmdq', 'gce_plat'))
    text += MODEL
    text += macro(poll, 'poll_timeout_us_atomic')
    text += '#define readl_poll_timeout_atomic(addr,val,cond,delay,timeout) poll_timeout_us_atomic((val)=readl(addr),cond,delay,timeout,false)\n'
    text += ''.join(block(core, n) for n in ('add_to_rbuf', 'msg_submit', 'tx_tick', 'mbox_chan_received_data',
                                           'mbox_client_txdone', 'mbox_send_message', 'mbox_flush'))
    text += ''.join(block(source, n) for n in ('cmdq_convert_gce_addr', 'cmdq_revert_gce_addr',
        'cmdq_thread_suspend', 'cmdq_thread_resume', 'cmdq_thread_reset', 'cmdq_thread_disable',
        'cmdq_thread_invalidate_fetched_data', 'cmdq_task_insert_into_thread', 'cmdq_thread_is_in_wfe',
        'cmdq_task_exec_done', 'cmdq_task_handle_error', 'cmdq_thread_irq_handler', 'cmdq_mbox_send_data', 'cmdq_mbox_flush'))
    text += TESTS
    directory = ROOT/'out/cmdq-flush-tests'
    directory.mkdir(exist_ok=True)
    generated = directory/'flush.c'
    generated.write_text(text)
    binary = generated.with_suffix('')
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
        '-Wno-unused-function', '-Wno-sign-compare', '-fsanitize=address,undefined',
        '-fno-pie', '-no-pie', '-O1', '-g', str(generated), '-o', str(binary)], check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'drivers/mailbox/mtk-cmdq-mailbox.c')
    args = parser.parse_args()
    subprocess.run([str(build(args.source))], check=True)


if __name__ == '__main__':
    main()
