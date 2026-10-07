#!/usr/bin/env python3
"""Exercise MT6765/MT8183 transfer and IRQ callbacks with MMIO/DMA stubs."""
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = ROOT/'src/mainline/drivers/i2c/busses/i2c-mt65xx.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()
matches = re.findall(r'\.compatible\s*=\s*"mediatek,mt6765-i2c"\s*,\s*\.data\s*=\s*&(\w+)',s)
assert matches == ['mt6765_compat'], 'MT6765 must select the tested compatibility data'


def block(prefix):
    start = s.index(prefix)
    end = s.index('\n}',start)+2
    if s[end:end+1] == ';':
        end += 1
    return s[start:end]+'\n'


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
typedef int irqreturn_t;
#define __iomem
#define IRQ_HANDLED 1
#define I2C_M_RD 1
#define I2C_MAX_STANDARD_MODE_FREQ 100000U
#define I2C_MAX_FAST_MODE_PLUS_FREQ 1000000U
#define DMA_TO_DEVICE 1
#define DMA_FROM_DEVICE 2
#define dev_dbg(...) ((void)0)
#define dev_err(...) ((void)0)
#define udelay(n) ((void)0)
#define upper_32_bits(n) ((u32)((uint64_t)(n) >> 32))
#define readw_poll_timeout(p,v,c,d,t) ((v) = readw(p), (c) ? 0 : -ETIMEDOUT)
struct device { int unused; };
struct completion { unsigned int done; };
struct i2c_timings { unsigned int scl_int_delay_ns; };
struct clk_bulk_data { void *clk; };
struct i2c_adapter { unsigned long timeout; };
struct i2c_msg { u16 addr, flags, len; u8 *buf; };
struct i2c_adapter_quirks { int unused; };
static const struct i2c_adapter_quirks mt8183_i2c_quirks;
'''
defines = '\n'.join(re.findall(r'^#define (?:I2C_\w+|MAX_\w+)[^\n]*',s,re.M))
declarations = ''.join(block(prefix) for prefix in [
    'enum i2c_mt65xx_clks {', 'enum DMA_REGS_OFFSET {', 'enum mtk_trans_op {',
    'enum I2C_REGS_OFFSET {', 'static const u16 mt_i2c_regs_v2[]',
    'static const u16 mt_i2c_regs_mt6765[]', 'struct mtk_i2c_compatible {',
    'struct mtk_i2c_ac_timing {', 'struct mtk_i2c {',
    'static const struct mtk_i2c_compatible mt8183_compat',
    'static const struct mtk_i2c_compatible mt6765_compat',
])
stubs = r'''
static u32 controller[0x1000/4], dma[0x100/4];
static struct mtk_i2c *current;
static bool expect_gate, dma_active, transfer_started;
static unsigned int gate_writes, starts, maps, unmaps, gets, releases, resets, copies;
static unsigned int injected_irq;
static int get_failure, map_failure;
static u8 buffers[2][16];
static u16 readw(void *address)
{
    u16 value;
    memcpy(&value,address,sizeof(value));
    return value;
}
static void writew(u16 value, void *address)
{
    ptrdiff_t offset = (u8 *)address - (u8 *)controller;
    assert(offset >= 0 && offset < 0x1000 && !(offset & 1));
    if (offset == 0x40) {
        assert(expect_gate && value == 1);
        gate_writes++;
    }
    if (offset == 0x24 && value & 1) {
        if (expect_gate) {
            assert(gate_writes == starts + 1 && readw((u8 *)controller + 0x40) == 1);
            assert(readw((u8 *)controller + 0x0c) == 0);
        } else
            assert(!gate_writes);
        starts++;
        transfer_started = true;
    }
    if (offset == 0x0c)
        value = readw(address) & ~value; /* W1C interrupt status */
    memcpy(address,&value,sizeof(value));
}
static void writel(u32 value, void *address)
{
    ptrdiff_t offset = (u8 *)address - (u8 *)dma;
    assert(offset >= 0 && offset < 0x100 && !(offset & 3));
    if (offset == 0x08 && value == 1)
        dma_active = true;
    if (offset == 0x0c && value & 2) {
        dma_active = false;
        resets++;
    }
    memcpy(address,&value,sizeof(value));
}
static u16 i2c_8bit_addr_from_msg(const struct i2c_msg *msg)
{
    return (msg->addr << 1) | !!(msg->flags & I2C_M_RD);
}
static void complete(struct completion *done) { done->done++; }
static void reinit_completion(struct completion *done) { done->done = 0; }
static void i2c_dump_register(struct mtk_i2c *i2c) { assert(i2c == current); }
static unsigned long wait_for_completion_timeout(struct completion *, unsigned long);
static u8 *i2c_get_dma_safe_msg_buf(struct i2c_msg *msg, unsigned int threshold)
{
    assert(threshold == 1 && msg->len <= sizeof(buffers[0]));
    assert(gets < 2);
    gets++;
    return (int)gets == get_failure ? NULL : buffers[gets-1];
}
static void i2c_put_dma_safe_msg_buf(u8 *buf, struct i2c_msg *msg, bool success)
{
    assert(!dma_active && msg->len <= sizeof(buffers[0]));
    if (!buf)
        return;
    releases++;
    if (success)
        copies++;
}
static dma_addr_t dma_map_single(struct device *dev, void *buf, size_t len, int dir)
{
    assert(dev == current->dev && buf && len && (dir == 1 || dir == 2));
    maps++;
    return (int)maps == map_failure ? UINT64_MAX : 0x100000000ULL + maps * 0x1000;
}
static bool dma_mapping_error(struct device *dev, dma_addr_t address)
{
    assert(dev == current->dev);
    return address == UINT64_MAX;
}
static void dma_unmap_single(struct device *dev, dma_addr_t address, size_t len, int dir)
{
    assert(dev == current->dev && address != UINT64_MAX && len && (dir == 1 || dir == 2));
    assert(!dma_active); /* Reset must precede unmapping a failed transfer. */
    unmaps++;
}
'''
body = ''.join(block(prefix) for prefix in [
    'static u16 mtk_i2c_readw(', 'static void mtk_i2c_writew(',
    'static u16 mtk_i2c_irq_mask(', 'static void mtk_i2c_start(',
    'static int mtk_i2c_transfer_error(', 'static void mtk_i2c_init_hw(',
    'static int mtk_i2c_do_transfer(', 'static irqreturn_t mtk_i2c_irq(',
])
checks = r'''
static void inject_irq(unsigned int status)
{
    u16 value = status;
    memcpy((u8 *)controller+0x0c,&value,sizeof(value));
    assert(mtk_i2c_irq(0,current) == IRQ_HANDLED);
    assert(readw((u8 *)controller+0x0c) == 0);
}
static unsigned long wait_for_completion_timeout(struct completion *done, unsigned long timeout)
{
    assert(done == &current->msg_complete && timeout == 200);
    if (expect_gate)
        assert(readw((u8 *)controller+0x08) == (current->auto_restart ? 0x13f : 0x12f));
    if (!injected_irq)
        return 0;
    assert(readw((u8 *)controller+0x08) & injected_irq);
    /* Completion-only means DMA has finished. Errors may leave it active. */
    if (injected_irq == 1 || injected_irq == 16)
        dma_active = false;
    inject_irq(injected_irq);
    if (!expect_gate && injected_irq == 2) {
        assert(!done->done); /* Preserve legacy ACK then completion sequence. */
        inject_irq(1);
    }
    assert(done->done == 1);
    return 1;
}
static void setup(struct mtk_i2c *i2c, bool mt6765)
{
    memset(i2c,0,sizeof(*i2c));
    memset(controller,0,sizeof(controller));
    memset(dma,0,sizeof(dma));
    current = i2c;
    i2c->dev_comp = mt6765 ? &mt6765_compat : &mt8183_compat;
    i2c->base = controller;
    i2c->pdmabase = dma;
    i2c->adap.timeout = 200;
    i2c->speed_hz = 100000;
    expect_gate = mt6765;
    dma_active = transfer_started = false;
    gate_writes = starts = maps = unmaps = gets = releases = resets = copies = 0;
    get_failure = map_failure = 0;
    injected_irq = 1;
    if (mt6765) {
        u16 stale = 0x1ff;
        memcpy((u8 *)controller+0x0c,&stale,sizeof(stale));
    }
}
int main(void)
{
    struct mtk_i2c i2c;
    u8 tx[3] = {0}, rx[5] = {0};
    struct i2c_msg msgs[2] = {{.addr=0x15,.len=3,.buf=tx},
                            {.addr=0x15,.flags=1,.len=5,.buf=rx}};
    const struct { unsigned int irq; int error; } cases[] = {
        {1,0}, {0,-ETIMEDOUT}, {2,-ENXIO}, {4,-ENXIO}, {8,-EAGAIN},
        {32,-ETIMEDOUT}, {65,-EIO}, {129,-EIO}, {256,-EIO},
        {257,-EIO}, {3,-ENXIO}, {288,-ETIMEDOUT}
    };
    unsigned int runs = 0;
    assert(mt6765_compat.regs[OFFSET_MCU_INTR] == 0x40);
    assert(mt6765_compat.regs[OFFSET_MULTI_DMA] == 0xf8c);
    setup(&i2c,true);
    writew(0x1ff,(u8 *)controller+0x08);
    mtk_i2c_init_hw(&i2c);
    assert(readw((u8 *)controller+0x08) == 0 && readw((u8 *)controller+0x0c) == 0);
    assert(resets == 1 && !starts && !gate_writes);
    for (unsigned int c = 0; c < sizeof(cases)/sizeof(cases[0]); c++) {
        for (int op = 1; op <= 3; op++) {
            setup(&i2c,true); i2c.op = op; injected_irq = cases[c].irq;
            i2c.auto_restart = op != I2C_MASTER_WRRD;
            assert(mtk_i2c_irq_mask(&i2c) == (op == I2C_MASTER_WRRD ? 0x12f : 0x13f));
            assert(mtk_i2c_do_transfer(&i2c,op == 2 ? msgs+1 : msgs,op == 3 ? 2 : 1,0)
                   == cases[c].error);
            assert(starts == 1 && gate_writes == 1 && !dma_active);
            assert(maps == (op == 3 ? 2U : 1U) && unmaps == maps && releases == maps);
            assert(copies == (cases[c].error ? 0 : maps));
            assert(resets == !!cases[c].error);
            assert(readw((u8 *)controller+0x08) == 0);
            runs++;
        }
    }
    setup(&i2c,true); i2c.auto_restart = 1; i2c.op = I2C_MASTER_WR;
    injected_irq = 16;
    assert(mtk_i2c_do_transfer(&i2c,msgs,2,1) == 0);
    assert(readw((u8 *)controller+0x24) == 0xc001);
    i2c.op = I2C_MASTER_RD; injected_irq = 1;
    assert(mtk_i2c_do_transfer(&i2c,msgs+1,2,0) == 0);
    assert(readw((u8 *)controller+0x24) == 0x4001);
    assert(starts == 2 && gate_writes == 2 && maps == 2 && unmaps == 2 && !resets);
    for (int failed = 1; failed <= 2; failed++) {
        for (int mapping = 0; mapping < 2; mapping++) {
            setup(&i2c,true); i2c.op = I2C_MASTER_WRRD;
            if (mapping) map_failure = failed; else get_failure = failed;
            assert(mtk_i2c_do_transfer(&i2c,msgs,2,0) == -ENOMEM);
            assert(!starts && !gate_writes && !dma_active);
            assert(unmaps == maps - (mapping ? 1U : 0U));
            assert(!copies);
        }
    }
    /* MT8183 does not acquire an AP-gate write or new terminal IRQ bits. */
    for (int failed = 0; failed < 2; failed++) {
        setup(&i2c,false); i2c.op = I2C_MASTER_RD; injected_irq = failed ? 2 : 1;
        assert(mtk_i2c_irq_mask(&i2c) == 0x0f);
        assert(mtk_i2c_do_transfer(&i2c,msgs+1,1,0) == (failed ? -ENXIO : 0));
        assert(!gate_writes && starts == 1 && resets == (unsigned int)failed);
    }
    for (int variant = 0; variant < 2; variant++) {
        setup(&i2c,variant); i2c.auto_restart = 1; i2c.ignore_restart_irq = true;
        inject_irq(16);
        assert(!i2c.msg_complete.done && !i2c.ignore_restart_irq && !i2c.irq_stat);
        assert(starts == 1 && gate_writes == (unsigned int)variant);
        inject_irq(1);
        assert(i2c.msg_complete.done == 1);
    }
    setup(&i2c,true); i2c.auto_restart = 1; i2c.ignore_restart_irq = true;
    inject_irq(16 | 256); /* A fault must not be discarded as a master-code IRQ. */
    assert(i2c.msg_complete.done == 1 && !starts);
    assert(mtk_i2c_transfer_error(&i2c,true) == -EIO);
    printf("PASS: %u MT6765 DMA transfers, terminal faults, AP gate, cleanup order,\n",runs);
    puts("      allocation/map failures, repeated starts and legacy MT8183 IRQ behavior");
    return 0;
}
'''
out = ROOT/'out/i2c-irq-tests'
out.mkdir(parents=True,exist_ok=True)
(out/'irq.c').write_text(prelude + defines + '\n' + declarations + stubs + body + checks)
subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                '-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer',
                str(out/'irq.c'),'-o',str(out/'irq')],check=True)
subprocess.run([str(out/'irq')],check=True)
print('Production driver code with modeled MMIO/DMA; physical IRQ delivery and DMA remain untested.')
