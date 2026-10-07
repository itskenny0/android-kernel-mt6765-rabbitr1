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
#define HZ_PER_GHZ 1000000000U
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define div_u64(n,d) ((n)/(d))
#define I2C_MAX_FAST_MODE_FREQ 400000U
#define I2C_MAX_HIGH_SPEED_MODE_FREQ 3400000U
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
struct i2c_adapter_quirks;
struct i2c_adapter {
    unsigned long timeout;
    const struct i2c_adapter_quirks *quirks;
    void *bus_regulator;
    bool suspended;
};
struct i2c_msg { u16 addr, flags, len; u8 *buf; };
#define I2C_AQ_COMB_WRITE_THEN_READ 15
#define I2C_AQ_NO_ZERO_LEN 96
struct i2c_adapter_quirks {
    unsigned int flags, max_num_msgs, max_comb_1st_msg_len, max_comb_2nd_msg_len;
};
struct device_node { int unused; };
struct arm_smccc_res { unsigned long a0; };
#define CONFIG_ARM64 1
#define ARM_SMCCC_SMC_64 1
#define ARM_SMCCC_FAST_CALL 1
#define ARM_SMCCC_OWNER_SIP 2
#define ARM_SMCCC_CALL_VAL(t,c,o,f) ((unsigned long)(t)<<31 | (c)<<30 | (o)<<24 | (f))
#include "/rabbitr1/src/mainline/include/linux/soc/mediatek/mtk_sip_svc.h"
static const struct i2c_adapter_quirks mt8183_i2c_quirks;
'''
defines = '\n'.join(re.findall(r'^#define (?:I2C_\w+|MAX_\w+)[^\n]*',s,re.M))
declarations = ''.join(block(prefix) for prefix in [
    'enum i2c_mt65xx_clks {', 'enum DMA_REGS_OFFSET {', 'enum mtk_trans_op {',
    'enum I2C_REGS_OFFSET {', 'static const u16 mt_i2c_regs_v2[]',
    'static const u16 mt_i2c_regs_mt6765[]', 'struct mtk_i2c_compatible {',
    'struct mtk_i2c_ac_timing {', 'struct mtk_i2c {',
    'struct i2c_spec_values {',
    'static const struct i2c_spec_values standard_mode_spec',
    'static const struct i2c_spec_values fast_mode_spec',
    'static const struct i2c_spec_values fast_mode_plus_spec',
    'static const struct mtk_i2c_compatible mt8183_compat',
    'static const struct mtk_i2c_compatible mt6765_compat',
    'static const struct i2c_adapter_quirks mt6765_channel_quirks',
])
stubs = r'''
static u32 controller[0x1000/4], dma[0x100/4];
static struct mtk_i2c *current;
static bool expect_gate, dma_active, transfer_started;
static unsigned int gate_writes, starts, maps, unmaps, gets, releases, resets, copies;
static unsigned int injected_irq, global_resets, kicks, controller_writes;
static int clocks_prepared, clocks_enabled, clock_failure;
static unsigned int parent_clock;
static u16 expected_timing, expected_ltiming, expected_timeout;
static unsigned long clk_get_rate(void *clk) { assert(clocks_enabled); return parent_clock; }
static unsigned long smc_reply;
static unsigned int smc_calls, resume_calls, dt_id;
static bool dt_secure, dt_malformed, check_clocks;
static struct device test_device;
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
    controller_writes++;
    if (check_clocks)
        assert(clocks_enabled && (!current->ch_offset || smc_calls));
    if (offset == 0x50 && value == 1)
        global_resets++;
    assert(!(current->ch_offset && offset == 0x150));
    if (current->ch_offset && offset == 0x24 && value == 2)
        kicks++;
    ptrdiff_t bank = current->ch_offset;
    if (offset == bank + 0x40) {
        assert(expect_gate && value == 1);
        gate_writes++;
    }
    if (offset == bank + 0x24 && value & 1) {
        if (expect_gate) {
            assert(gate_writes == starts + 1 && readw((u8 *)controller + bank + 0x40) == 1);
            assert(readw((u8 *)controller + bank + 0x0c) == 0);
        } else
            assert(!gate_writes);
        if (bank) {
            assert(value == 1); /* No unverified multi-restart bits on the AP bank. */
            assert(readw((u8 *)controller+bank+0x38) == 5);
            assert(readw((u8 *)controller+bank+0x10) ==
                   (current->op == I2C_MASTER_WRRD ? 0x33e : 0x32c));
        }
        if (expect_gate && dma_active) {
            assert(readw((u8 *)controller+bank+0x20) == expected_timing);
            assert(readw((u8 *)controller+bank+0x2c) == expected_ltiming);
            assert(readw((u8 *)controller+bank+0x48) == 0x404);
            assert(readw((u8 *)controller+bank+0x34) == 3);
            assert(readw((u8 *)controller+bank+0x4c) == expected_timeout);
        }
        starts++;
        transfer_started = true;
    }
    if (offset == 0x0c || offset == bank + 0x0c)
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
static int clk_bulk_prepare_enable(int count, struct clk_bulk_data *clocks)
{
    assert(count == I2C_MT65XX_CLK_MAX && clocks == current->clocks);
    assert(!clocks_prepared && !clocks_enabled);
    if (clock_failure) return clock_failure;
    clocks_prepared = clocks_enabled = 1;
    return 0;
}
static int clk_bulk_enable(int count, struct clk_bulk_data *clocks)
{
    assert(count == I2C_MT65XX_CLK_MAX && clocks == current->clocks);
    assert(clocks_prepared == 1 && !clocks_enabled);
    clocks_enabled = 1;
    return 0;
}
static void clk_bulk_disable(int count, struct clk_bulk_data *clocks)
{
    assert(count == I2C_MT65XX_CLK_MAX && clocks == current->clocks && clocks_enabled == 1);
    clocks_enabled = 0;
}
static void clk_bulk_unprepare(int count, struct clk_bulk_data *clocks)
{
    assert(count == I2C_MT65XX_CLK_MAX && clocks == current->clocks);
    assert(clocks_prepared == 1 && !clocks_enabled);
    clocks_prepared = 0;
}
static void clk_bulk_disable_unprepare(int count, struct clk_bulk_data *clocks)
{
    clk_bulk_disable(count,clocks);
    clk_bulk_unprepare(count,clocks);
}
static void arm_smccc_smc(unsigned long fn, unsigned long id, unsigned long reg,
                         unsigned long value, unsigned long a, unsigned long b,
                         unsigned long c, unsigned long d, struct arm_smccc_res *res)
{
    assert(clocks_prepared == 1 && clocks_enabled == 1);
    assert(fn == 0xc20002a0 && id == current->secure_id);
    assert(id == 2 || id == 3 || id == 4 || id == 6);
    assert(reg == 0xf8c && value == 2 && !a && !b && !c && !d);
    smc_calls++;
    res->a0 = smc_reply;
}
static struct mtk_i2c *dev_get_drvdata(struct device *dev)
{
    assert(dev == current->dev);
    return current;
}
static struct mtk_i2c *i2c_get_adapdata(struct i2c_adapter *adap)
{
    assert(adap == &current->adap);
    return current;
}
static void i2c_mark_adapter_suspended(struct i2c_adapter *adap) { adap->suspended = true; }
static void i2c_mark_adapter_resumed(struct i2c_adapter *adap)
{
    assert(clocks_prepared == 1 && !clocks_enabled && !smc_reply);
    adap->suspended = false;
    resume_calls++;
}
static int regulator_enable(void *regulator) { assert(regulator); return 0; }
static int regulator_disable(void *regulator) { assert(regulator); return 0; }
static bool of_property_present(struct device_node *node, const char *name)
{
    assert(!strcmp(name,"mediatek,secure-id"));
    return dt_secure;
}
static bool of_property_read_bool(struct device_node *node, const char *name) { return false; }
static int of_property_read_u32(struct device_node *node, const char *name, unsigned int *value)
{
    if (!strcmp(name,"mediatek,secure-id")) {
        assert(dt_secure);
        if (dt_malformed) return -EINVAL;
        *value = dt_id;
    } else if (!strcmp(name,"clock-div")) *value = current->dev_comp == &mt6765_compat ? 5 : 1;
    else { assert(!strcmp(name,"clock-frequency")); *value = 100000; }
    return 0;
}
static void i2c_parse_fw_timings(struct device *dev, struct i2c_timings *timing, bool defaults) {}
'''
body = s[s.index('static const struct i2c_spec_values *mtk_i2c_get_spec('):
         s.index('static void i2c_dump_register(')]
body += ''.join(block(prefix) for prefix in [
    'static u16 mtk_i2c_readw_bank(', 'static void mtk_i2c_writew_bank(',
    'static u16 mtk_i2c_readw(', 'static void mtk_i2c_writew(',
    'static u16 mtk_i2c_irq_mask(', 'static void mtk_i2c_start(',
    'static int mtk_i2c_transfer_error(', 'static void mtk_i2c_configure(',
    'static void mtk_i2c_reset_dma(', 'static void mtk_i2c_init_hw(',
    'static void mtk_i2c_recover(', 'static int mtk_i2c_enable_channel(',
    'static int mtk_i2c_prepare_hw(',
    'static int mtk_i2c_do_transfer(', 'static int mtk_i2c_transfer(',
    'static irqreturn_t mtk_i2c_irq(', 'static int mtk_i2c_parse_dt(',
    'static int mtk_i2c_suspend_noirq(', 'static int mtk_i2c_resume_noirq(',
])
checks = r'''
static void inject_irq(unsigned int status)
{
    u16 value = status;
    memcpy((u8 *)controller+current->ch_offset+0x0c,&value,sizeof(value));
    assert(mtk_i2c_irq(0,current) == IRQ_HANDLED);
    assert(readw((u8 *)controller+current->ch_offset+0x0c) == 0);
}
static unsigned long wait_for_completion_timeout(struct completion *done, unsigned long timeout)
{
    assert(done == &current->msg_complete && timeout == 200);
    if (expect_gate) {
        unsigned int expected = current->ch_offset ? 0x129 :
                                (current->auto_restart ? 0x13f : 0x12f);
        assert(readw((u8 *)controller+current->ch_offset+0x08) == expected);
    }
    if (!injected_irq)
        return 0;
    assert(readw((u8 *)controller+current->ch_offset+0x08) & injected_irq);
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
    i2c->dev = &test_device;
    i2c->dev_comp = mt6765 ? &mt6765_compat : &mt8183_compat;
    i2c->base = controller;
    i2c->pdmabase = dma;
    i2c->adap.timeout = 200;
    i2c->speed_hz = 100000;
    i2c->clk_src_div = mt6765 ? 5 : 1;
    parent_clock = 26000000;
    expected_timing = 0x1b; expected_ltiming = 0x1a; expected_timeout = 386;
    assert(!mtk_i2c_set_speed(i2c,parent_clock));
    expect_gate = mt6765;
    dma_active = transfer_started = false;
    gate_writes = starts = maps = unmaps = gets = releases = resets = copies = 0;
    get_failure = map_failure = 0;
    injected_irq = 1;
    global_resets = kicks = controller_writes = 0;
    clocks_prepared = clocks_enabled = clock_failure = 0;
    smc_reply = smc_calls = resume_calls = dt_id = 0;
    dt_secure = dt_malformed = check_clocks = false;
    if (mt6765) {
        u16 stale = 0x1ff;
        memcpy((u8 *)controller+0x0c,&stale,sizeof(stale));
    }
}
static void setup_channel(struct mtk_i2c *i2c)
{
    setup(i2c,true);
    dt_secure = true; dt_id = 4;
    assert(mtk_i2c_parse_dt(NULL,i2c) == 0);
    assert(i2c->ch_offset == 0x100 && i2c->secure_id == 4);
    assert(i2c->adap.quirks == &mt6765_channel_quirks);
    assert(i2c->adap.quirks->flags == (I2C_AQ_COMB_WRITE_THEN_READ | I2C_AQ_NO_ZERO_LEN));
    assert(i2c->adap.quirks->max_num_msgs == 1);
    u16 stale = 0x1ff;
    memcpy((u8 *)controller+0x10c,&stale,sizeof(stale));
    memset((u8 *)controller+0x200,0xa5,0x100); /* CCU's bank must be untouched. */
}
static unsigned int test_channel_transfers(struct i2c_msg *msgs)
{
    struct mtk_i2c i2c;
    const struct { unsigned int irq; int error; bool global; } cases[] = {
        {1,0,false}, {0,-ETIMEDOUT,true}, {3,-ENXIO,false}, {5,-ENXIO,false},
        {8,-EAGAIN,true}, {32,-ETIMEDOUT,true}, {65,-EIO,false}, {129,-EIO,false},
        {256,-EIO,true}, {257,-EIO,true}, {288,-ETIMEDOUT,true}, {33,-ETIMEDOUT,false}
    };
    unsigned int runs = 0;
    for (unsigned int c = 0; c < sizeof(cases)/sizeof(cases[0]); c++) {
        for (int op = 1; op <= 3; op++) {
            setup_channel(&i2c); i2c.op = op; injected_irq = cases[c].irq;
            assert(mtk_i2c_irq_mask(&i2c) == 0x129);
            assert(mtk_i2c_do_transfer(&i2c,op == 2 ? msgs+1 : msgs,op == 3 ? 2 : 1,0)
                   == cases[c].error);
            assert(starts == 1 && gate_writes == 1 && !dma_active);
            assert(resets == 1U + !!cases[c].error);
            assert(global_resets == (unsigned int)cases[c].global);
            assert(kicks == (unsigned int)(cases[c].global && cases[c].irq));
            assert(maps == (op == 3 ? 2U : 1U) && unmaps == maps && releases == maps);
            assert(copies == (cases[c].error ? 0 : maps));
            assert(readw((u8 *)controller+0x108) == 0);
            for (unsigned int at = 0x200; at < 0x300; at++)
                assert(((u8 *)controller)[at] == 0xa5);
            runs++;
        }
    }
    for (int failed = 1; failed <= 2; failed++) {
        for (int mapping = 0; mapping < 2; mapping++) {
            setup_channel(&i2c); i2c.op = I2C_MASTER_WRRD;
            if (mapping) map_failure = failed; else get_failure = failed;
            assert(mtk_i2c_do_transfer(&i2c,msgs,2,0) == -ENOMEM);
            assert(!starts && !gate_writes && !dma_active && !global_resets);
            assert(unmaps == maps - (mapping ? 1U : 0U) && !copies);
            assert(readw((u8 *)controller+0x108) == 0);
        }
    }
    setup_channel(&i2c);
    inject_irq(2); /* Masked NACK must be latched until an enabled event. */
    assert(i2c.irq_stat == 2 && !i2c.msg_complete.done);
    inject_irq(1);
    assert(i2c.msg_complete.done == 1 && mtk_i2c_transfer_error(&i2c,true) == -ENXIO);
    /* Software timeout only kicks arbitration if this bank has ownership. */
    setup_channel(&i2c);
    writew(2,(u8 *)controller+0x124);
    mtk_i2c_recover(&i2c,false);
    assert(global_resets == 1 && kicks == 1);
    /* Exercise the adapter entry point, including WRRD selection and clocks. */
    for (int op = 1; op <= 3; op++) {
        setup_channel(&i2c); check_clocks = true;
        assert(mtk_i2c_prepare_hw(&i2c) == 0 && smc_calls == 1);
        int num = op == 3 ? 2 : 1;
        assert(mtk_i2c_transfer(&i2c.adap,op == 2 ? msgs+1 : msgs,num) == num);
        assert(i2c.op == (enum mtk_trans_op)op && !i2c.auto_restart);
        assert(starts == 1 && global_resets == 1 && !clocks_enabled && clocks_prepared);
        assert(mtk_i2c_suspend_noirq(i2c.dev) == 0 && !clocks_prepared);
    }
    return runs;
}
static void test_channel_setup(void)
{
    struct mtk_i2c i2c;
    for (unsigned int id = 0; id <= 7; id++) {
        setup(&i2c,true); dt_secure = true; dt_id = id;
        bool valid = id == 2 || id == 3 || id == 4 || id == 6;
        assert(mtk_i2c_parse_dt(NULL,&i2c) == (valid ? 0 : -EINVAL));
        if (!valid) { assert(!i2c.ch_offset); continue; }
        check_clocks = true;
        assert(mtk_i2c_prepare_hw(&i2c) == 0 && smc_calls == 1 && global_resets == 1);
        assert(mtk_i2c_suspend_noirq(i2c.dev) == 0);
    }
    setup(&i2c,false); dt_secure = true; dt_id = 4;
    assert(mtk_i2c_parse_dt(NULL,&i2c) == -EINVAL && !i2c.ch_offset);
    setup(&i2c,true); dt_secure = dt_malformed = true; dt_id = 4;
    assert(mtk_i2c_parse_dt(NULL,&i2c) == -EINVAL && !i2c.ch_offset);
    for (int variant = 0; variant < 2; variant++) {
        setup(&i2c,variant); check_clocks = true;
        assert(mtk_i2c_parse_dt(NULL,&i2c) == 0 && !i2c.ch_offset);
        assert(mtk_i2c_prepare_hw(&i2c) == 0 && !smc_calls);
        assert(mtk_i2c_suspend_noirq(i2c.dev) == 0);
    }
    setup_channel(&i2c); check_clocks = true; clock_failure = -EIO;
    assert(mtk_i2c_prepare_hw(&i2c) == -EIO);
    assert(!clocks_prepared && !clocks_enabled && !i2c.clocks_prepared);
    assert(!smc_calls && !controller_writes);
    setup_channel(&i2c); check_clocks = true; smc_reply = (unsigned long)-1;
    assert(mtk_i2c_prepare_hw(&i2c) == -EIO);
    assert(smc_calls == 1 && !controller_writes);
    assert(!clocks_prepared && !clocks_enabled && !i2c.clocks_prepared);
    setup_channel(&i2c); check_clocks = true;
    assert(mtk_i2c_prepare_hw(&i2c) == 0);
    assert(mtk_i2c_suspend_noirq(i2c.dev) == 0 && i2c.adap.suspended);
    smc_reply = (unsigned long)-1;
    assert(mtk_i2c_resume_noirq(i2c.dev) == -EIO && i2c.adap.suspended);
    assert(!resume_calls && !i2c.clocks_prepared && !clocks_prepared && !clocks_enabled);
    assert(mtk_i2c_suspend_noirq(i2c.dev) == 0); /* No double unprepare. */
    smc_reply = 0;
    assert(mtk_i2c_resume_noirq(i2c.dev) == 0 && !i2c.adap.suspended);
    assert(resume_calls == 1 && smc_calls == 3 && i2c.clocks_prepared);
    assert(mtk_i2c_suspend_noirq(i2c.dev) == 0);
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
    /* A changed parent must refresh timing before START in either bank. */
    for (unsigned int channel = 0; channel < 2; channel++) {
        if (channel) setup_channel(&i2c); else setup(&i2c,true);
        check_clocks = true;
        assert(!mtk_i2c_prepare_hw(&i2c));
        parent_clock = 52000000;
        expected_timing = 0x35; expected_ltiming = 0x34; expected_timeout = 393;
        assert(mtk_i2c_transfer(&i2c.adap,msgs,1) == 1);
        assert(starts == 1 && !clocks_enabled && clocks_prepared);
        parent_clock = 0;
        unsigned int writes = controller_writes;
        assert(mtk_i2c_transfer(&i2c.adap,msgs,1) == -EINVAL);
        assert(starts == 1 && controller_writes == writes && !clocks_enabled);
        assert(!mtk_i2c_suspend_noirq(i2c.dev));
    }
    unsigned int channel_runs = test_channel_transfers(msgs);
    test_channel_setup();
    printf("PASS: %u MT6765 DMA transfers, terminal faults, AP gate, cleanup order,\n",runs);
    puts("      allocation/map failures, repeated starts and legacy MT8183 IRQ behavior");
    printf("PASS: %u AP-bank transfers, shared recovery, secure setup and suspend/resume failures\n",channel_runs);
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
