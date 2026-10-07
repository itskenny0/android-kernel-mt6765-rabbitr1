#!/usr/bin/env python3
"""Execute the CST836 transport, report and power callbacks with host stubs."""
import os
from pathlib import Path
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = ROOT/'src/mainline/drivers/input/touchscreen/hynitron-cst836.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()


def function(prefix):
    start = s.index(prefix)
    return s[start:s.index('\n}', start)+2] + '\n'


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
typedef int irqreturn_t;
#define BIT(n) (1U << (n))
#define IRQ_HANDLED 1
#define MT_TOOL_FINGER 0
#define dev_dbg_ratelimited(...) ((void)0)
#define dev_dbg(...) ((void)0)
struct device { int unused; };
struct i2c_client { struct device dev; int irq; void *data; };
struct gpio_desc { int asserted; };
struct touchscreen_properties { unsigned int max_x, max_y; };
struct input_dev {
    bool active[2], seen[2];
    unsigned int x[2], y[2], slot, frames, syncs, positions;
};
static u16 get_unaligned_be16(const u8 *p) { return ((u16)p[0] << 8) | p[1]; }
static inline u16 get_unaligned_le16(const u8 *p) { return ((u16)p[1] << 8) | p[0]; }
static struct i2c_client *to_i2c_client(struct device *dev) { return (void *)dev; }
static void *i2c_get_clientdata(struct i2c_client *client) { return client->data; }
static unsigned int calls, delays, disabled, enabled;
static int fail_at = -1, failure = -EIO;
static bool version_read;
static u8 packet[15];
static int i2c_master_send(struct i2c_client *client, const void *data, int len)
{
    const u8 *reg = data;
    assert(client->irq == 17 && !(calls & 1) && len == 1);
    assert(*reg == (version_read ? 0xa6 : calls / 2 * 5));
    return (int)calls++ == fail_at ? failure : len;
}
static int i2c_master_recv(struct i2c_client *client, void *data, int len)
{
    assert(client->irq == 17 && (calls & 1));
    assert(len == (version_read ? 2 : 5));
    assert(calls < (version_read ? 2 : 6));
    if ((int)calls++ == fail_at)
        return failure;
    memcpy(data, packet + (version_read ? 0 : (calls / 2 - 1) * 5), len);
    return len;
}
static void input_mt_slot(struct input_dev *input, int slot)
{
    assert(slot >= 0 && slot < 2);
    input->slot = slot;
}
static void input_mt_report_slot_state(struct input_dev *input, int tool, bool active)
{
    assert(tool == MT_TOOL_FINGER);
    input->seen[input->slot] = true;
    input->active[input->slot] = active;
}
static void touchscreen_report_pos(struct input_dev *input,
                                  const struct touchscreen_properties *prop,
                                  unsigned int x, unsigned int y, bool multitouch)
{
    assert(multitouch && x <= prop->max_x && y <= prop->max_y);
    input->positions++;
    input->x[input->slot] = x;
    input->y[input->slot] = y;
}
static void input_mt_sync_frame(struct input_dev *input)
{
    /* Model INPUT_MT_DROP_UNUSED and the frame boundary. */
    for (int i = 0; i < 2; i++) {
        if (!input->seen[i])
            input->active[i] = false;
        input->seen[i] = false;
    }
    input->frames++;
}
static void input_sync(struct input_dev *input) { input->syncs++; }
static void gpiod_set_value_cansleep(struct gpio_desc *gpio, int value)
{
    assert(value == 0 || value == 1);
    gpio->asserted = value;
}
static void msleep(unsigned int ms)
{
    assert(ms == (delays % 2 ? 100 : 20));
    delays++;
}
static void disable_irq(int irq) { assert(irq == 17); disabled++; }
static void enable_irq(int irq) { assert(irq == 17); enabled++; }
'''
body = s[s.index('#define CST836_MAX_CONTACTS'):s.index('static int cst836_probe(')]
body += function('static int cst836_suspend(')
body += function('static int cst836_resume(')
checks = r'''
static void run_irq(struct cst836 *ts, const u8 *frame, unsigned int active_mask,
                    unsigned int positions, unsigned int transfers)
{
    memcpy(packet, frame, sizeof(packet));
    calls = 0;
    version_read = false;
    unsigned int frames = ts->input->frames, syncs = ts->input->syncs;
    ts->input->positions = 0;
    assert(cst836_irq(17, ts) == IRQ_HANDLED);
    assert(calls == transfers);
    assert(ts->input->frames == frames + 1 && ts->input->syncs == syncs + 1);
    assert(ts->input->positions == positions);
    for (int i = 0; i < 2; i++)
        assert(ts->input->active[i] == !!(active_mask & (1U << i)));
}
int main(void)
{
    struct input_dev input = {0};
    struct gpio_desc reset = {0};
    struct i2c_client client = {.irq = 17};
    struct cst836 ts = {.client = &client, .input = &input, .reset = &reset,
                        .prop = {.max_x = 479, .max_y = 639}, .irq_enabled = true};
    client.data = &ts;
    /* ID 1 DOWN at (479,639), then ID 0 CONTACT at (258,517). */
    const u8 both[15] = {0,0,2, 0x01,0xdf,0x12,0x7f,0x81,0x30,
                                0x81,0x02,0x02,0x05,0x44,0x50};
    /* Only ID 0 remains. The second record is stale and must be ignored. */
    const u8 one[15] = {0,0,1, 0x80,0x03,0x00,0x04,0,0,
                               0x81,0xff,0x12,0xff,0,0};
    const u8 up[15] = {0,0,1, 0x4f,0xff,0x0f,0xff,0,0};
    const u8 empty[15] = {0};
    run_irq(&ts,both,3,2,6);
    assert(input.x[1] == 479 && input.y[1] == 639);
    assert(input.x[0] == 258 && input.y[0] == 517);
    run_irq(&ts,one,1,1,6);
    assert(input.x[0] == 3 && input.y[0] == 4);
    run_irq(&ts,up,0,0,6);
    run_irq(&ts,both,3,2,6);
    run_irq(&ts,empty,0,0,6);

    u8 bad[15];
    for (int count = 3; count < 16; count++) {
        memcpy(bad,both,15); bad[2] = count;
        run_irq(&ts,bad,0,0,6);
    }
    for (int id = 2; id < 16; id++) {
        memcpy(bad,both,15); bad[11] = (id << 4) | 2;
        run_irq(&ts,bad,0,0,6);
    }
    for (int bit = 4; bit <= 6; bit++) {
        memcpy(bad,both,15); bad[0] |= 1 << bit;
        run_irq(&ts,bad,0,0,6);
    }
    memcpy(bad,both,15); bad[11] |= 0x10; /* duplicate ID */
    run_irq(&ts,bad,0,0,6);
    memcpy(bad,both,15); bad[9] |= 0xc0; /* reserved event */
    run_irq(&ts,bad,0,0,6);
    memcpy(bad,both,15); bad[4]++; /* X = 480 */
    run_irq(&ts,bad,0,0,6);
    memcpy(bad,both,15); bad[6]++; /* Y = 640 */
    run_irq(&ts,bad,0,0,6);

    /* Every transfer may fail or complete short. No partial frame escapes. */
    for (int step = 0; step < 6; step++) {
        int errors[] = {-EREMOTEIO, -ETIMEDOUT, 0, step & 1 ? 4 : 0};
        for (unsigned int i = 0; i < sizeof(errors)/sizeof(errors[0]); i++) {
            fail_at = -1;
            run_irq(&ts,both,3,2,6);
            fail_at = step; failure = errors[i];
            run_irq(&ts,both,0,0,step+1);
        }
    }
    fail_at = -1;
    run_irq(&ts,both,3,2,6);
    assert(cst836_suspend(&client.dev) == 0);
    assert(disabled == 1 && reset.asserted);
    assert(!input.active[0] && !input.active[1]);
    calls = 0; version_read = true;
    assert(cst836_resume(&client.dev) == 0);
    assert(calls == 2 && delays == 2 && !reset.asserted && enabled == 1);
    assert(cst836_suspend(&client.dev) == 0);
    calls = 0; fail_at = 1; failure = -ETIMEDOUT;
    assert(cst836_resume(&client.dev) == -ETIMEDOUT);
    assert(calls == 2 && delays == 4 && reset.asserted && enabled == 1);
    /* PM may start another cycle after a failed resume. Do not nest disables. */
    assert(cst836_suspend(&client.dev) == 0);
    assert(disabled == 2);
    calls = 0; fail_at = -1;
    assert(cst836_resume(&client.dev) == 0);
    assert(enabled == 2 && ts.irq_enabled && !reset.asserted);

    /* Decode all 12-bit coordinate values, independent of panel size. */
    struct cst836_contact contacts[2];
    for (unsigned int xy = 0; xy < 4096; xy++) {
        u8 frame[15] = {0,0,1, xy >> 8,xy, xy >> 8,xy};
        assert(cst836_decode(frame,contacts,4095,4095) == 1);
        assert(contacts[0].x == xy && contacts[0].y == xy && contacts[0].id == 0);
    }
    puts("PASS: CST836 two contacts, slot releases, invalid frames, 12-bit coordinates,");
    puts("      STOP-separated reads, short/error transfers and suspend/resume callbacks");
    return 0;
}
'''
out = ROOT/'out/cst836-tests'
out.mkdir(parents=True, exist_ok=True)
(out/'touch.c').write_text(prelude + body + checks)
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-parameter', '-O1', '-g', '-fsanitize=address,undefined',
                '-fno-omit-frame-pointer', str(out/'touch.c'), '-o', str(out/'touch')], check=True)
subprocess.run([str(out/'touch')], check=True)
print('Production callbacks with I2C/input/GPIO stubs; no touch hardware was accessed.')
