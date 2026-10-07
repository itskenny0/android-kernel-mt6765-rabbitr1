#!/usr/bin/env python3
"""Run production backlight callbacks with property, regmap and GPIO fixtures."""
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = ROOT/'src/mainline/drivers/video/backlight/mt6370-backlight.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()


def block(prefix):
    start = s.index(prefix)
    end = s.index('\n}', start)+2
    if s[end:end+1] == ';': end += 1
    return s[start:end]+'\n'


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctz(m))
#define clamp_val(v,l,h) ((v) < (l) ? (l) : ((v) > (h) ? (h) : (v)))
#define min_t(t,a,b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define dev_err(...) ((void)0)
#define dev_err_probe(dev,err,...) (err)
#define BACKLIGHT_SCALE_NON_LINEAR 1
#define BACKLIGHT_SCALE_LINEAR 2
enum { MT6370_VID_COMMON = 1, MT6370_VID_6372 };
struct device { unsigned int match; };
struct gpio_desc { int value; };
struct regmap { u8 data[0x200]; };
struct backlight_properties { unsigned int brightness, max_brightness, scale; };
struct backlight_device { void *data; unsigned int brightness; };
static struct device dev;
static struct gpio_desc gpio;
static struct regmap map;
static struct backlight_device bl;
static struct backlight_properties props;
static unsigned int flags, writes, reads, gpio_writes, addresses[4];
static int fail_write, fail_read;
static int channels, hysteresis, ovp, ocp, max_brightness, default_brightness;
static void *bl_get_data(struct backlight_device *b) { return b->data; }
static int backlight_get_brightness(struct backlight_device *b) { return b->brightness; }
static void *device_get_match_data(struct device *d) { return (void *)(uintptr_t)d->match; }
static void gpiod_set_value(struct gpio_desc *g, int value)
{
    if (!g) return;
    assert(value == 0 || value == 1); g->value = value; gpio_writes++;
}
static bool device_property_read_bool(struct device *d, const char *key)
{
    assert(d == &dev);
    const char *names[] = {"mediatek,bled-pwm-enable", "mediatek,bled-pwm-hys-enable",
        "mediatek,bled-ovp-shutdown", "mediatek,bled-ocp-shutdown",
        "mediatek,bled-exponential-mode-enable"};
    for (unsigned int i = 0; i < 5; i++)
        if (!strcmp(key,names[i])) return !!(flags & (1U << i));
    assert(false); return false;
}
static int device_property_read_u8(struct device *d, const char *key, u8 *value)
{
    assert(d == &dev);
    int result;
    if (!strcmp(key,"mediatek,bled-channel-use")) result = channels;
    else { assert(!strcmp(key,"mediatek,bled-pwm-hys-input-th-steps")); result = hysteresis; }
    if (result < 0) return -EINVAL;
    *value = result; return 0;
}
static int device_property_read_u32(struct device *d, const char *key, u32 *value)
{
    assert(d == &dev);
    int result;
    if (!strcmp(key,"mediatek,bled-ovp-microvolt")) result = ovp;
    else if (!strcmp(key,"mediatek,bled-ocp-microamp")) result = ocp;
    else if (!strcmp(key,"max-brightness")) result = max_brightness;
    else { assert(!strcmp(key,"default-brightness")); result = default_brightness; }
    if (result < 0) return -EINVAL;
    *value = result; return 0;
}
static int regmap_update_bits(struct regmap *m, unsigned int address,
                              unsigned int mask, unsigned int value)
{
    assert(m == &map && address >= 0x1a0 && address <= 0x1a5 && mask <= 255);
    assert(writes < 4); addresses[writes++] = address;
    if ((int)writes == fail_write) return -EIO;
    m->data[address] = (m->data[address] & ~mask) | (value & mask);
    return 0;
}
static int regmap_write(struct regmap *m, unsigned int address, unsigned int value)
{
    return regmap_update_bits(m,address,255,value);
}
/* Retained so the former raw-write implementation compiles for mutation tests. */
static int regmap_raw_write(struct regmap *m, unsigned int address, const u8 *v, unsigned int len)
{
    assert(len == 2);
    int ret = regmap_write(m,address,v[0]);
    return ret ? ret : regmap_write(m,address+1,v[1]);
}
static int regmap_read(struct regmap *m, unsigned int address, unsigned int *value)
{
    assert(m == &map && (address == 0x100 || address == 0x1a0));
    if ((int)++reads == fail_read) return -EIO;
    *value = m->data[address]; return 0;
}
static int regmap_raw_read(struct regmap *m, unsigned int address, u8 *value, unsigned int length)
{
    assert(m == &map && address == 0x1a4 && length == 2);
    if ((int)++reads == fail_read) return -EIO;
    memcpy(value,&m->data[address],length); return 0;
}
'''
prelude += '\n'.join(re.findall(r'^#define MT6370_\w+[^\n]*', s, re.M))+'\n'
body = block('struct mt6370_priv {')
body += ''.join(block(prefix) for prefix in [
    'static int mt6370_check_vendor_info(', 'static int mt6370_init_backlight_properties(',
    'static int mt6370_bl_update_status(', 'static int mt6370_bl_get_brightness(',
])
checks = r'''
static struct mt6370_priv priv;
static void setup(unsigned int chip, unsigned int seed)
{
    memset(&priv,0,sizeof(priv)); memset(&props,0,sizeof(props));
    memset(&map,seed,sizeof(map));
    map.data[0x100] = chip;
    writes = reads = gpio_writes = flags = 0;
    fail_write = fail_read = 0;
    channels = 15; hysteresis = ovp = ocp = max_brightness = default_brightness = -1;
    priv.dev = &dev; priv.regmap = &map;
    bl.data = &priv;
    dev.match = (chip >> 4 == 9 || chip >> 4 == 11) ? 2 : 1;
    assert(!mt6370_check_vendor_info(&priv));
    assert(priv.revision == (chip & 15));
    reads = 0;
}
static void check_init(unsigned int seed, unsigned int selection)
{
    const unsigned int steps[] = {1,4,16,64};
    setup(0xe2,seed);
    channels = 1 + seed % 15;
    hysteresis = steps[selection]; ovp = 17000000+selection*4000000;
    ocp = 900000+selection*300000; max_brightness = 512; default_brightness = 0;
    for (unsigned int f = 0; f < 64; f++) {
        memset(&map,seed,sizeof(map)); writes = 0;
        flags = f; priv.enable_gpio = f & 32 ? &gpio : NULL;
        assert(!mt6370_init_backlight_properties(&priv,&props));
        assert(writes == 3 && !gpio_writes);
        assert(addresses[0] == 0x1a2 && addresses[1] == 0x1a1 && addresses[2] == 0x1a0);
        assert(map.data[0x1a2] == ((seed & 0x78) | (f & 1 ? 0x80 : 0) |
               (f & 2 ? 4 : 0) | selection));
        assert(map.data[0x1a1] == ((seed & 0x11) | (f & 4 ? 0 : 0x80) |
               (f & 8 ? 0 : 8) | selection*0x20 | selection*2));
        assert(map.data[0x1a0] == ((seed & 0x41) | (f & 32 ? 0x80 : 0) |
               (f & 16 ? 0 : 2) | channels*4));
        assert(props.scale == (f & 16 ? BACKLIGHT_SCALE_NON_LINEAR : BACKLIGHT_SCALE_LINEAR));
        assert(props.max_brightness == 512 && props.brightness == 0);
        for (unsigned int at = 0; at < sizeof(map.data); at++)
            if (at < 0x1a0 || at > 0x1a2) assert(map.data[at] == seed);
    }
}
static void check_defaults(void)
{
    for (unsigned int seed = 0; seed < 256; seed++) {
        setup(0xe2,seed);
        assert(!mt6370_init_backlight_properties(&priv,&props));
        assert(map.data[0x1a2] == (seed & ~0x84));
        assert(map.data[0x1a1] == (seed | 0x88)); /* Omitted thresholds are retained. */
        assert(map.data[0x1a0] == ((seed & 0x41) | 0x3e));
        assert(props.max_brightness == 2048 && props.brightness == 2048);
    }
    setup(0x92,0); max_brightness = default_brightness = 20000;
    assert(!mt6370_init_backlight_properties(&priv,&props));
    assert(props.max_brightness == 16384 && props.brightness == 16384);
    setup(0xe2,0); max_brightness = 0; default_brightness = 100;
    assert(!mt6370_init_backlight_properties(&priv,&props));
    assert(!props.max_brightness && !props.brightness);
    /* Clamp and round the numeric fields, including zero encodings. */
    const struct { int h, v, c; unsigned int hp, vp, cp; } bounds[] = {
        {0,0,0,0,0,0}, {2,17000001,900001,1,1,1},
        {5,21000001,1200001,2,2,2}, {17,25000001,1500001,3,3,3},
        {255,100000000,100000000,3,3,3}
    };
    for (unsigned int i = 0; i < sizeof(bounds)/sizeof(bounds[0]); i++) {
        setup(0xe2,255); hysteresis = bounds[i].h; ovp = bounds[i].v; ocp = bounds[i].c;
        assert(!mt6370_init_backlight_properties(&priv,&props));
        assert(map.data[0x1a2] == (0x78 | bounds[i].hp));
        assert(map.data[0x1a1] == (0x99 | bounds[i].vp*32 | bounds[i].cp*2));
    }
}
static void check_errors(void)
{
    const unsigned int vendors[] = {8,10,14,15,9,11};
    for (unsigned int v = 0; v < 6; v++)
        for (unsigned int rev = 0; rev < 16; rev++)
            for (unsigned int request = 0; request < 4; request++) {
                setup(vendors[v]*16+rev,0x55); flags = request*4;
                int expected = rev <= 1 && request ? -EOPNOTSUPP : 0;
                assert(mt6370_init_backlight_properties(&priv,&props) == expected);
                assert(writes == (expected ? 0 : 3));
            }
    for (int ch = -1; ch <= 16; ch++) {
        setup(0xe2,255); channels = ch;
        int expected = ch <= 0 || ch > 15 ? -EINVAL : 0;
        assert(mt6370_init_backlight_properties(&priv,&props) == expected);
        assert(writes == (expected ? 0 : 3));
    }
    for (int failure = 1; failure <= 3; failure++) {
        setup(0xe2,255); fail_write = failure;
        assert(mt6370_init_backlight_properties(&priv,&props) == -EIO);
        assert(writes == (unsigned int)failure && !gpio_writes);
        assert(map.data[addresses[failure-1]] == 255);
    }
    setup(0xe2,0); fail_read = 1;
    assert(mt6370_check_vendor_info(&priv) == -EIO && !writes);
    setup(0xe2,0); dev.match = 2;
    assert(mt6370_check_vendor_info(&priv) == -EINVAL && !writes);
}
static void check_brightness(void)
{
    const unsigned int chips[] = {0xe2,0x92};
    for (unsigned int v = 0; v < 2; v++) {
        unsigned int limit = v ? 16384 : 2048, lowmask = v ? 63 : 7, shift = v ? 6 : 3;
        setup(chips[v],255); priv.enable_gpio = &gpio;
        for (unsigned int level = 0; level <= limit; level++) {
            writes = reads = gpio_writes = 0;
            memset(&map,255,sizeof(map)); bl.brightness = level;
            assert(!mt6370_bl_update_status(&bl));
            assert(writes == (level ? 3 : 1) && gpio_writes == 1 && gpio.value == !!level);
            assert(map.data[0x1a0] == (level ? 255 : 0xbf));
            if (level) {
                assert(addresses[0] == 0x1a4 && addresses[1] == 0x1a5 && addresses[2] == 0x1a0);
                assert(map.data[0x1a4] == ((255 & ~lowmask) | ((level-1) & lowmask)));
                assert(map.data[0x1a5] == (level-1) >> shift);
            } else assert(map.data[0x1a4] == 255 && map.data[0x1a5] == 255);
            assert(mt6370_bl_get_brightness(&bl) == (int)level);
        }
        for (int failure = 1; failure <= 3; failure++) {
            setup(chips[v],0xa0); priv.enable_gpio = &gpio; gpio.value = 0;
            fail_write = failure; bl.brightness = 512;
            assert(mt6370_bl_update_status(&bl) == -EIO);
            assert(writes == (unsigned int)failure);
            assert(gpio_writes == (unsigned int)(failure == 3));
            assert(map.data[addresses[failure-1]] == 0xa0);
        }
        for (int failure = 1; failure <= 2; failure++) {
            setup(chips[v],255); fail_read = failure;
            assert(mt6370_bl_get_brightness(&bl) == -EIO && !writes);
        }
    }
}
int main(void)
{
    for (unsigned int seed = 0; seed < 256; seed++)
        for (unsigned int selection = 0; selection < 4; selection++) check_init(seed,selection);
    check_defaults(); check_errors(); check_brightness();
    puts("PASS: 65536 backlight property/register cases, retained fields, mode and shutdown polarity");
    puts("PASS: early revisions, invalid channels, brightness round trips and regmap failures");
    return 0;
}
'''
out = ROOT/'out/mt6370-backlight-tests'
out.mkdir(parents=True, exist_ok=True)
(out/'backlight.c').write_text(prelude+body+checks)
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                '-O1', '-g', '-fno-pie', '-no-pie', '-fsanitize=address,undefined',
                str(out/'backlight.c'), '-o', str(out/'backlight')], check=True)
subprocess.run([str(out/'backlight')], check=True)
print('Production callbacks with modeled registers and GPIO; no backlight hardware was accessed.')
