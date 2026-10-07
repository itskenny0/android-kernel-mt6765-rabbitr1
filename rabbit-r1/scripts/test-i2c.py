#!/usr/bin/env python3
"""Run production I2C timing/init code with register stubs, without hardware.

The MT8183 clock model checks fixed-divider regressions. MT6765 register
programming is also compared with the shipped kernel by test-i2c-stock-timing.py.
An optional DTS input supports running the check against the original source.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/rabbitr1')
os.environ['TMPDIR'] = str(ROOT/'.tmp')
source = ROOT/'src/mainline/drivers/i2c/busses/i2c-mt65xx.c'
dts = ROOT/'src/mainline/arch/arm64/boot/dts/mediatek/mt6765.dtsi'
if len(sys.argv) == 2:
    dts = Path(sys.argv[1]).resolve()
    if not dts.is_relative_to(ROOT):
        raise SystemExit('DTS must be under /rabbitr1')
s = source.read_text()


def block(prefix):
    start = s.index(prefix)
    end = s.index('\n}', start) + 2
    if s[end:end+1] == ';':
        end += 1
    return s[start:end] + '\n'


nodes = re.findall(r'\bi2c([0-6]): i2c@[0-9a-f]+ \{(.*?)\n\t\t\};', dts.read_text(), re.S)
if len(nodes) != 7 or len({index for index, _ in nodes}) != 7:
    raise SystemExit('Expected all seven MT6765 I2C controllers')
dividers = []
for index, node in sorted(nodes):
    if '"mediatek,mt6765-i2c"' not in node:
        raise SystemExit('Review the timing harness when changing the I2C compatible')
    dividers.append(int(re.search(r'clock-div = <(\d+)>;', node)[1]))

prelude = r'''
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint16_t u16;
#define HZ_PER_GHZ 1000000000U
#define I2C_MAX_STANDARD_MODE_FREQ 100000U
#define I2C_MAX_FAST_MODE_FREQ 400000U
#define I2C_MAX_FAST_MODE_PLUS_FREQ 1000000U
#define I2C_MAX_HIGH_SPEED_MODE_FREQ 3400000U
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define GENMASK(h, l) ((~0U << (l)) & (~0U >> (31 - (h))))
#define div_u64(n, d) ((n) / (d))
#define dev_dbg(...) ((void)0)
#define udelay(n) ((void)0)
#define writel(v, p) ((void)(v), (void)(p))
struct i2c_adapter_quirks { int unused; };
static const struct i2c_adapter_quirks mt8183_i2c_quirks;
'''
defines = '\n'.join(re.findall(r'^#define (?:I2C_\w+|MAX_\w+)[^\n]*', s, re.M))
body = ''.join(block(prefix) for prefix in [
    'enum DMA_REGS_OFFSET {', 'enum I2C_REGS_OFFSET {',
    'static const u16 mt_i2c_regs_v2[]', 'static const u16 mt_i2c_regs_mt6765[]',
    'struct mtk_i2c_compatible {',
    'struct mtk_i2c_ac_timing {', 'struct i2c_spec_values {',
    'static const struct i2c_spec_values standard_mode_spec',
    'static const struct i2c_spec_values fast_mode_spec',
    'static const struct i2c_spec_values fast_mode_plus_spec',
    'static const struct mtk_i2c_compatible mt8183_compat',
    'static const struct mtk_i2c_compatible mt6765_compat',
])
body += r'''
struct mtk_i2c {
    const struct mtk_i2c_compatible *dev_comp;
    struct mtk_i2c_ac_timing ac_timing;
    struct { unsigned int scl_int_delay_ns; } timing_info;
    unsigned int clk_src_div, speed_hz;
    uintptr_t pdmabase;
    u16 timing_reg, high_speed_reg, ltiming_reg, ch_offset, hw_timeout;
    bool use_push_pull, have_pmic;
    u16 registers[0x1000 / 2];
};
static u16 mtk_i2c_readw_bank(struct mtk_i2c *i2c, u16 bank, enum I2C_REGS_OFFSET reg)
{
    return i2c->registers[(bank + i2c->dev_comp->regs[reg]) / 2];
}
static void mtk_i2c_writew_bank(struct mtk_i2c *i2c, u16 bank, u16 value, enum I2C_REGS_OFFSET reg)
{
    i2c->registers[(bank + i2c->dev_comp->regs[reg]) / 2] = value;
}
static u16 mtk_i2c_readw(struct mtk_i2c *i2c, enum I2C_REGS_OFFSET reg)
{
    return mtk_i2c_readw_bank(i2c, i2c->ch_offset, reg);
}
static void mtk_i2c_writew(struct mtk_i2c *i2c, u16 value, enum I2C_REGS_OFFSET reg)
{
    mtk_i2c_writew_bank(i2c, i2c->ch_offset, value, reg);
}
'''
body += block('static void mtk_i2c_configure(')
body += block('static void mtk_i2c_reset_dma(')
body += block('static void mtk_i2c_init_hw(')
body += s[s.index('static const struct i2c_spec_values *mtk_i2c_get_spec('):
          s.index('static void i2c_dump_register(')]
checks = 'static const unsigned int dt_dividers[] = {' + ','.join(map(str, dividers)) + '};\n'
checks += r'''
static int emit(unsigned int parent, unsigned int speed, unsigned int div, unsigned int bank)
{
    struct mtk_i2c i2c = {
        .dev_comp = &mt6765_compat, .clk_src_div = div, .speed_hz = speed,
        .ch_offset = bank,
    };
    int ret = mtk_i2c_set_speed(&i2c,parent);
    printf("%d",ret);
    if (!ret) {
        mtk_i2c_configure(&i2c,bank);
        const unsigned int regs[] = {0x20,0x2c,0x30,0x48,0x4c,0x28,0x1c,0x34};
        for (unsigned int i = 0; i < sizeof(regs)/sizeof(regs[0]); i++)
            printf(" %u",i2c.registers[(bank+regs[i])/2]);
    }
    putchar('\n');
    return 0;
}
int main(int argc, char **argv)
{
    if (argc == 5)
        return emit(strtoul(argv[1],NULL,0),strtoul(argv[2],NULL,0),
                    strtoul(argv[3],NULL,0),strtoul(argv[4],NULL,0));
    assert(argc == 1);
    /* Test clock-rate inputs, not measurements of a particular r1. */
    const unsigned int parents[] = {26000000, 65000000, 104000000, 124800000, 136500000};
    const unsigned int speeds[] = {100000, 400000};
    const struct mtk_i2c_compatible *variants[] = {&mt8183_compat};
    unsigned int count = 0;
    for (unsigned int variant = 0; variant < 1; variant++) {
    for (unsigned int bus = 0; bus < 7; bus++) {
        for (unsigned int p = 0; p < sizeof(parents) / sizeof(parents[0]); p++) {
            for (unsigned int f = 0; f < sizeof(speeds) / sizeof(speeds[0]); f++) {
                struct mtk_i2c i2c = {
                    .dev_comp = variants[variant],
                    .clk_src_div = 1, .speed_hz = speeds[f],
                };
                assert(mtk_i2c_set_speed(&i2c, parents[p]) == 0);
                mtk_i2c_init_hw(&i2c);
                unsigned int div = mtk_i2c_readw(&i2c, OFFSET_CLOCK_DIV) + 1;
                unsigned int high = mtk_i2c_readw(&i2c, OFFSET_TIMING);
                unsigned int low = mtk_i2c_readw(&i2c, OFFSET_LTIMING);
                unsigned int sample = ((high >> 8) & 7) + 1;
                unsigned int low_sample = ((low >> 6) & 7) + 1;
                /* Decode the fallback's count restriction independently. */
                int correction = div == 1 ? (sample == 1 ? 0 : -1) :
                                 (div == 2 && sample != 1 ? 0 : 1);
                unsigned int cycles = sample * ((high & 63) + (low & 63) + 2) -
                                      2 * correction;
                if (sample != low_sample || !cycles ||
                    (uint64_t)parents[p] > (uint64_t)speeds[f] * div * cycles) {
                    fprintf(stderr, "FAIL: i2c%u parent=%u requested=%u fixed=%u "
                            "CLOCK_DIV=%#x TIMING=%#x LTIMING=%#x model_hz=%.1f\n",
                            bus, parents[p], speeds[f], dt_dividers[bus], div - 1,
                            high, low, (double)parents[p] / div / cycles);
                    return 1;
                }
                count++;
            }
        }
    }
    }
    printf("PASS: %u MT8183 timing/init cases; programmed dividers respect requested rates\n", count);
    count = 0;
    for (unsigned int bus = 0; bus < 7; bus++) {
        assert(dt_dividers[bus] == 5);
        for (unsigned int p = 0; p < sizeof(parents)/sizeof(parents[0]); p++) {
            for (unsigned int f = 0; f < sizeof(speeds)/sizeof(speeds[0]); f++) {
                for (unsigned int bank = 0; bank <= 0x100; bank += 0x100) {
                    struct mtk_i2c i2c = {.dev_comp = &mt6765_compat,
                        .clk_src_div = dt_dividers[bus], .speed_hz = speeds[f], .ch_offset = bank};
                    for (unsigned int at = 0; at < sizeof(i2c.registers)/sizeof(u16); at++)
                        i2c.registers[at] = 0xa55a;
                    assert(!mtk_i2c_set_speed(&i2c,parents[p]));
                    mtk_i2c_init_hw(&i2c);
                    mtk_i2c_configure(&i2c,bank);
                    assert(i2c.registers[(bank+0x48)/2] == 0x404);
                    assert(i2c.registers[(bank+0x4c)/2] > 0);
                    assert(i2c.registers[(bank+0x20)/2] & 1);
                    assert(i2c.registers[(bank+0x3c)/2] == 0xa55a);
                    assert(i2c.registers[(bank+0x90)/2] == 0xa55a);
                    count++;
                }
            }
        }
    }
    printf("PASS: %u MT6765 bank/divider/timeout setup cases; no MT8183-only timing writes\n",count);
    struct mtk_i2c i2c = {.dev_comp = &mt6765_compat, .clk_src_div = 5, .speed_hz = 100000};
    assert(mtk_i2c_set_speed(&i2c,0) == -EINVAL);
    i2c.speed_hz = 0; assert(mtk_i2c_set_speed(&i2c,26000000) == -EINVAL);
    i2c.speed_hz = 999; assert(mtk_i2c_set_speed(&i2c,26000000) == -EINVAL);
    i2c.speed_hz = 3400001; assert(mtk_i2c_set_speed(&i2c,26000000) == -EINVAL);
    i2c.speed_hz = 100000;
    i2c.clk_src_div = 0; assert(mtk_i2c_set_speed(&i2c,26000000) == -EINVAL);
    i2c.clk_src_div = 9; assert(mtk_i2c_set_speed(&i2c,26000000) == -EINVAL);
    i2c.clk_src_div = 5;
    assert(!mtk_i2c_set_speed(&i2c,64000000));
    assert(i2c.timing_reg == 0x11f && i2c.ltiming_reg == 0x5f);
    puts("PASS: invalid timing rejected; one-sample 64-step overflow avoided");
    return 0;
}
'''
out = ROOT/'out/i2c-tests'
out.mkdir(parents=True, exist_ok=True)
(out/'timing.c').write_text(prelude + defines + '\n' + body + checks)
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g', '-fsanitize=address,undefined',
                str(out/'timing.c'), '-o', str(out/'timing')], check=True)
subprocess.run([str(out/'timing')], check=True)
print('No I2C transfers executed. Electrical timing and DMA still need hardware tests.')
