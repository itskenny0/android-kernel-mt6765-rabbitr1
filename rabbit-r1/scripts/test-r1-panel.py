#!/usr/bin/env python3
"""Compare production panel callbacks with shipped instructions; no DSI hardware."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
import sys
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
IMAGE_SHA256 = '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
source = ROOT/'src/mainline/drivers/gpu/drm/panel/panel-rabbit-r1.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')


def stock_trace(raw, function):
    # Run the complete selected panel callback and its table interpreter.
    # Delay, reset, DSI transport, ftrace and memset calls are modeled.
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    bias, obj, stub = 0x1000000, 0x20000000, 0x30000000
    uc.mem_map(bias, 0x1a00000)  # includes lcm_util in zero-initialized BSS
    uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000)
    uc.mem_map(stub, 0x1000)
    stack, params, stop = obj+0x8000, obj+0x1000, stub+0x100
    util = bias+0x19e9dc0
    for offset,target in [(0,stub), (32,stub+4), (40,stub+8), (200,stub+12)]:
        uc.mem_write(util+offset, struct.pack('<Q',target))
    uc.reg_write(UC_ARM64_REG_SP,stack)
    uc.reg_write(UC_ARM64_REG_X30,stop)
    uc.reg_write(UC_ARM64_REG_X0,params)
    events = []
    def code(uc,address,size,_):
        x0,x1,x2,x3,x4 = [uc.reg_read(r) for r in
            (UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3,UC_ARM64_REG_X4)]
        if address == bias+0x1f01c:
            pass  # _mcount
        elif address == bias+0xf1e480:
            assert x0 == params and x1 == 0 and x2 == 0x518
            uc.mem_write(x0,bytes(x2))
        elif address == stub:
            assert x0 in (0,1)
            events.append(f'reset:{x0}')
        elif address == stub+8:
            events.append(f'delay:{x0}')
        elif address == stub+12:
            assert x0 == 0 and x4 == 1 and 0 <= x1 <= 255 and x2 <= 64
            data = bytes([x1])+bytes(uc.mem_read(x3,x2))
            # DSI_set_cmdq_V2 in the published host sends >=0xb0 as generic.
            # The panel/table emulation ends at that transport boundary.
            events.append(('dcs:' if x1 < 0xb0 else 'generic:')+data.hex())
        else:
            assert 0x6eed08 <= address-bias < 0x6eefb8 or \
                   0x6ef19c <= address-bias < 0x6ef254, hex(address-bias)
            return
        uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
    def write(uc,access,address,size,value,_):
        assert obj <= address and address+size <= obj+0x10000, hex(address)
    uc.hook_add(UC_HOOK_CODE,code)
    uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+function,stop,count=10000)
    assert uc.reg_read(UC_ARM64_REG_PC) == stop
    assert uc.reg_read(UC_ARM64_REG_SP) == stack
    return events, bytes(uc.mem_read(params,0x518))


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
typedef uint8_t u8;
typedef uint16_t u16;
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define dev_err(...) ((void)0)
#define dev_warn(...) ((void)0)
struct device { int unused; };
struct mipi_dsi_device { struct device dev; };
struct drm_panel { bool prepared; };
struct gpio_desc { int logical; };
struct drm_display_mode {
    int clock, hdisplay, hsync_start, hsync_end, htotal;
    int vdisplay, vsync_start, vsync_end, vtotal;
};
static unsigned int calls;
static int fail_at, fail_kind, gpio_fail;
static void msleep(unsigned int ms) { printf("delay:%u\n",ms); }
static void gpiod_set_value_cansleep(struct gpio_desc *g, int value)
{
    assert(value == 0 || value == 1);
    g->logical = value;
    printf("reset:%d\n",!value); /* physical level for active-low wiring */
}
static int gpiod_direction_output(struct gpio_desc *g, int value)
{
    if (gpio_fail) return -EBUSY;
    gpiod_set_value_cansleep(g,value);
    return 0;
}
static ssize_t transfer(const char *kind, const void *buffer, size_t len)
{
    const u8 *data = buffer;
    assert(len >= 1 && len <= 17);
    printf("%s:",kind);
    for (size_t i=0;i<len;i++) printf("%02x",data[i]);
    putchar('\n');
    if (++calls == (unsigned int)fail_at)
        return fail_kind ? (ssize_t)len-1 : -ETIMEDOUT;
    return len;
}
static ssize_t mipi_dsi_dcs_write_buffer(struct mipi_dsi_device *d, const void *data, size_t len)
{ (void)d; return transfer("dcs",data,len); }
static ssize_t mipi_dsi_generic_write(struct mipi_dsi_device *d, const void *data, size_t len)
{ (void)d; return transfer("generic",data,len); }
'''
main = r'''
int main(int argc, char **argv)
{
    assert(argc == 5);
    struct mipi_dsi_device dsi = {0};
    struct gpio_desc gpio = { .logical = -1 };
    struct r1_panel ctx = { .dsi = &dsi, .reset = &gpio };
    assert(r1_panel_enable(&ctx.panel) == -EPERM);
    ctx.panel.prepared = true;
    assert(r1_panel_enable(&ctx.panel) == 0);
    ctx.panel.prepared = false;
    fail_at = atoi(argv[2]); fail_kind = atoi(argv[3]); gpio_fail = atoi(argv[4]);
    int ret;
    if (!strcmp(argv[1],"mode")) {
        const struct drm_display_mode *m = &r1_panel_mode;
        printf("%d %d %d %d %d %d %d %d %d\n",m->clock,m->hdisplay,
            m->hsync_start,m->hsync_end,m->htotal,m->vdisplay,m->vsync_start,m->vsync_end,m->vtotal);
        return 0;
    }
    if (!strcmp(argv[1],"prepare")) ret = r1_panel_prepare(&ctx.panel);
    else if (!strcmp(argv[1],"unprepare")) ret = r1_panel_unprepare(&ctx.panel);
    else {
        assert(!strcmp(argv[1],"cycle"));
        assert(!r1_panel_prepare(&ctx.panel));
        assert(!r1_panel_unprepare(&ctx.panel));
        ret = r1_panel_prepare(&ctx.panel);
    }
    printf("result:%d\n",ret);
    return 0;
}
'''


def main_test():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert len(raw) == 26019840 and hashlib.sha256(raw).hexdigest() == IMAGE_SHA256
    expected, _ = stock_trace(raw,0x6eee04)
    suspend, _ = stock_trace(raw,0x6eee80)
    resume, _ = stock_trace(raw,0x6eeed8)
    assert resume == ['delay:10']+expected+['delay:10']
    assert stock_trace(raw,0x6eef14)[0] == ['reset:0','delay:10']
    assert stock_trace(raw,0x6eef7c)[0] == ['reset:0','delay:15']
    _, params = stock_trace(raw,0x6eed08)
    fields = {0:2,8:1,12:1,24:480,28:640,96:0,472:1,492:2,508:3,
              516:2,520:1440,524:256,532:2,536:14,540:26,548:640,
              552:20,556:20,560:20,568:480,668:130,692:1,868:0}
    for offset,value in fields.items(): assert struct.unpack_from('<I',params,offset)[0] == value
    src = source.read_text().split('static int r1_panel_get_modes')[0]
    src = re.sub(r'^#include .*\n','',src,flags=re.M)
    harness = ROOT/'out/r1-panel-host.c'
    binary = ROOT/'out/r1-panel-host'
    harness.write_text(prelude+src+main)
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-sign-compare',
                    '-fsanitize=address,undefined','-fno-pie','-no-pie','-g','-O1',str(harness),'-o',str(binary)],check=True)
    def run(action,fail=0,short=0,gpio=0):
        return subprocess.check_output([str(binary),action,str(fail),str(short),str(gpio)],text=True).splitlines()
    assert run('prepare') == expected+['result:0']
    assert run('unprepare') == suspend+['result:0']
    assert run('cycle') == expected+suspend+expected+['result:0']
    # Every command can fail: stop immediately, assert reset, keep the error.
    commands = [i for i,event in enumerate(expected) if event.startswith(('dcs:','generic:'))]
    assert len(commands) == 39
    for short,error in [(0,-110),(1,-5)]:
        for n,index in enumerate(commands,1):
            assert run('prepare',n,short) == expected[:index+1]+['reset:0','delay:120',f'result:{error}']
        for n in (1,2):
            # Reset quiesces the panel even if a sleep command failed. The
            # panel core must clear prepared so a later prepare can retry.
            assert run('unprepare',n,short) == suspend+['result:0']
    assert run('prepare',gpio=1) == ['result:-16']
    mode = list(map(int,run('mode')[0].split()))
    clock,ha,hss,hse,ht,va,vss,vse,vt = mode
    def word(offset): return struct.unpack_from('<I',params,offset)[0]
    assert (ha,hss-ha,hse-hss,ht-hse) == tuple(word(x) for x in (568,560,552,556))
    assert (va,vss-va,vse-vss,vt-vse) == tuple(word(x) for x in (548,540,532,536))
    assert clock == round(word(668)*2*1000*word(492)/24)
    spec = importlib.util.spec_from_file_location('validate',ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec); spec.loader.exec_module(validate)
    stock = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())
    for level in (0,1):
        node = stock[f'/pinctrl@1000b000/lcm_rst_out{level}_gpio/pins_cmd_dat']
        assert node['pinmux'] == struct.pack('>I',45<<8)
        assert ('output-high' if level else 'output-low') in node
    nodes = validate.fdt_nodes((ROOT/'dist/mainline/mt6765-rabbit-r1.dtb').read_bytes())
    dsi_path = '/soc/dsi@14014000'
    panel = nodes[dsi_path+'/panel@0']
    assert panel['compatible'] == b'rabbit,r1-panel\0'
    assert panel['reset-gpios'] == nodes['/soc/pin-controller@10005000']['phandle']+struct.pack('>II',45,1)
    assert panel['backlight'] == nodes['/soc/i2c@11016000/pmic@34/backlight']['phandle']
    assert nodes[dsi_path+'/port/endpoint']['remote-endpoint'] == nodes[dsi_path+'/panel@0/port/endpoint']['phandle']
    assert nodes[dsi_path+'/panel@0/port/endpoint']['remote-endpoint'] == nodes[dsi_path+'/port/endpoint']['phandle']
    # Do not silently activate the unaudited display path.
    assert nodes[dsi_path]['status'] == b'disabled\0'
    assert nodes['/soc/dsi-phy@11c80000']['status'] == b'disabled\0'
    assert nodes['/soc/i2c@11016000/pmic@34/backlight']['status'] == b'disabled\0'
    result = {'stock_image_sha256':IMAGE_SHA256,'init_trace':expected,'suspend_trace':suspend,
              'drm_mode':mode,'failed_command_cases':len(commands)*2+4,'reset_gpio':45,
              'scope':'selected stock callbacks and production driver with modeled transports; no hardware'}
    (ROOT/'out/r1-panel-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS: panel init/suspend traces match shipped instructions; 39 commands, delays and reset levels')
    print('PASS: 82 command failures, GPIO failure, prepare/unprepare cycle, mode translation and stock reset wiring')
    print('DSI transport, supply rails, refresh, controller identity and actual display remain untested.')


if __name__ == '__main__':
    main_test()
