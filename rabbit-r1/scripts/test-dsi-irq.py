#!/usr/bin/env python3
"""Check MT6765 DSI status acknowledgement against stock code and late events."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21,
    UC_ARM64_REG_X22, UC_ARM64_REG_X23, UC_ARM64_REG_X24, UC_ARM64_REG_X25,
    UC_ARM64_REG_X26, UC_ARM64_REG_X27, UC_ARM64_REG_X28, UC_ARM64_REG_X29,
    UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')


def stock_irq(raw, status, late, cmdq):
    bias, obj, mmio, stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, 0x2000000); uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000); uc.mem_map(mmio, 0x1000); uc.mem_map(stop, 0x2000)
    uc.mem_write(mmio+0xc, struct.pack('<I', status))
    # CPU reads clear RD_RDY; stock CMDQ/ESD reads leave it for the reader.
    uc.mem_write(bias+0x1782b40, struct.pack('<I', cmdq))
    # Model one registered DSI callback; execute the real dispatch loop.
    uc.mem_write(bias+0x1a275f8, bytes(0x50+29*0x50))
    uc.mem_write(bias+0x1a27648+13*0x50, struct.pack('<Q', stop+0x1000))
    uc.mem_write(bias+0x1a27f84, bytes(4))
    saved = [UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21,
             UC_ARM64_REG_X22, UC_ARM64_REG_X23, UC_ARM64_REG_X24,
             UC_ARM64_REG_X25, UC_ARM64_REG_X26, UC_ARM64_REG_X27,
             UC_ARM64_REG_X28, UC_ARM64_REG_X29]
    for i, reg in enumerate(saved): uc.reg_write(reg, 0x12340000+i)
    uc.reg_write(UC_ARM64_REG_SP, obj+0x8000)
    uc.reg_write(UC_ARM64_REG_X30, stop)
    uc.reg_write(UC_ARM64_REG_X0, 0x10d)
    ack, reads, callbacks = [], [], []
    writeback = None
    remaining = status

    def code(uc, address, size, _):
        nonlocal writeback
        if writeback is not None:
            uc.mem_write(mmio+0xc, struct.pack('<I', writeback)); writeback = None
        at = address-bias
        if address == stop+0x1000:
            callbacks.append([uc.reg_read(UC_ARM64_REG_X0), uc.reg_read(UC_ARM64_REG_X1)])
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        elif at in (0x1f01c, 0x7440ac, 0x743f94, 0x732564, 0x746e68, 0x69f6bc):
            # ftrace, IRQ/base lookup, logging switch, profiling metadata/transport.
            if at == 0x7440ac:
                assert uc.reg_read(UC_ARM64_REG_X0) == 13
                uc.reg_write(UC_ARM64_REG_X0, 0x10d)
            elif at == 0x743f94:
                assert uc.reg_read(UC_ARM64_REG_X0) == 13
                uc.reg_write(UC_ARM64_REG_X0, mmio)
            elif at != 0x1f01c:
                uc.reg_write(UC_ARM64_REG_X0, obj if at == 0x746e68 else 0)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert 0x742260 <= at < 0x7422c4 or 0x7424e8 <= at < 0x742624, hex(at)

    def read(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            assert address == mmio+0xc and size == 4
            reads.append(address-mmio)

    def write(uc, access, address, size, value, _):
        nonlocal writeback, remaining
        if address == mmio+0xc:
            assert size == 4
            ack.append(value)
            # The new event arrives after the snapshot, just before the write.
            remaining = (status | late) & value
            writeback = remaining
        else:
            assert obj+0x7000 <= address and address+size <= obj+0x8000, hex(address)

    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_READ, read)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.emu_start(bias+0x742260, stop, count=1000)
    assert uc.reg_read(UC_ARM64_REG_PC) == stop and uc.reg_read(UC_ARM64_REG_SP) == obj+0x8000
    assert [uc.reg_read(reg) for reg in saved] == [0x12340000+i for i in range(len(saved))]
    assert uc.reg_read(UC_ARM64_REG_X0) == 1
    mask = status & (0xfffe if cmdq == 1 else 0xffff)
    assert ack == [(~mask) & 0xffffffff] and reads == [0xc], (ack, reads)
    assert callbacks == [[13, status & 0xffff]], callbacks
    assert struct.unpack('<I', uc.mem_read(mmio+0xc, 4))[0] == remaining
    return dict(status=status, late=late, cmdq=cmdq, ack=ack[0], remaining=remaining)


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define IRQ_NONE 0
typedef u32 atomic_t;
static void atomic_or(u32 bits, atomic_t *v) { *v |= bits; }
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
'''
MODEL = r'''
struct mtk_dsi {
    void *regs;
    const struct mtk_dsi_driver_data *driver_data;
    u32 irq_data;
    unsigned int irq_wait_queue;
};
static unsigned char regs[0x200];
static unsigned int reads, racks, acks, wakes, busy, stage;
static u32 late, ack_value;
static struct mtk_dsi *current;
static u32 load(unsigned int off) { u32 v; memcpy(&v,regs+off,4); return v; }
static void save(unsigned int off,u32 v) { memcpy(regs+off,&v,4); }
static u32 readl(const void *p)
{
    uintptr_t off=(const unsigned char *)p-regs;
    assert(off==0xc || off==0x84);
    u32 v=load(off);
    if (off==0xc) {
        assert(reads<10); /* A finite model, not a test of permanently busy hardware. */
        if (busy) v|=0x80000000;
        if (reads && busy) busy--;
        if (!reads && stage==0) save(off,load(off)|late);
        reads++;
    }
    return v;
}
static void writel(u32 v,void *p)
{
    uintptr_t off=(unsigned char *)p-regs;
    if (off==0xc) {
        assert(!acks && !wakes);
        ack_value=v; acks++;
        u32 events=load(off) & 0xffff;
        if (stage==1) events|=late;
        save(off,events&v); /* Status latches are write-zero-to-clear. */
    } else {
        assert(off==0x84 && !acks && !wakes);
        assert(v==0x12345679); /* Preserve other RACK fields. */
        save(off,v); racks++;
    }
}
static void wake_up_interruptible(unsigned int *q)
{
    assert(q==&current->irq_wait_queue && acks==1 && !wakes);
    wakes++;
}
'''
MAIN = r'''
static void check(struct mtk_dsi *d,u32 initial,u32 arrival,unsigned int when,
                  unsigned int latency,bool native)
{
    current=d; d->irq_data=0xa5000000;
    memset(regs,0,sizeof(regs)); save(0xc,initial); save(0x84,0x12345678);
    reads=racks=acks=wakes=0; late=arrival; stage=when; busy=latency;
    u32 snapshot=initial&0xb;
    assert(mtk_dsi_irq(73,d)==(snapshot?IRQ_HANDLED:IRQ_NONE));
    assert(acks==!!snapshot && wakes==!!snapshot);
    assert(d->irq_data==(0xa5000000|snapshot));
    assert(racks==(!native && snapshot?latency+1:0));
    if (native) assert(reads==1); /* BUSY never delays the hard IRQ. */
    if (snapshot) {
        if (native) {
            assert(load(0xc)==((initial|arrival)&~snapshot));
            assert(ack_value==~snapshot);
        } else {
            /* Unchanged shared backend: late events between RMW and write are lost. */
            u32 visible=initial|(when==0?arrival:0);
            assert(ack_value==(visible&~snapshot));
            assert(load(0xc)==(visible&~snapshot));
        }
    } else {
        assert(reads==1 && !racks);
        assert((load(0xc)&0xffff)==(initial|(when==0?arrival:0)));
    }
    if (native) {
        /* A preserved, previously unseen completion must be delivered next time. */
        u32 pending=load(0xc)&0xffff;
        reads=racks=acks=wakes=busy=0; late=0; stage=1;
        save(0xc,pending);
        assert(mtk_dsi_irq(73,d)==((pending&0xb)?IRQ_HANDLED:IRQ_NONE));
        assert(d->irq_data==(0xa5000000|snapshot|(pending&0xb)));
        assert(wakes==!!(pending&0xb) && acks==!!(pending&0xb));
        assert(load(0xc)==(pending&~0xb));
    }
}
int main(void)
{
    struct mtk_dsi d={.regs=regs,.driver_data=&mt6765_dsi_driver_data};
    check(&d,2,8,1,0,true); /* VM_DONE arriving immediately before CMD_DONE ack. */
    const u32 arrivals[]={0,1,2,8,0xb,0xffff};
    unsigned int cases=0;
    for (u32 status=0;status<0x10000;status++)
        for (unsigned int i=0;i<sizeof(arrivals)/sizeof(arrivals[0]);i++)
            for (unsigned int when=0;when<2;when++) {
                /* Same-bit events coalesce in a latch and cannot be counted separately. */
                check(&d,status,arrivals[i]&~status,when,status%4,true); cases++;
            }
    d.driver_data=&mt8183_dsi_driver_data;
    for (u32 status=0;status<16;status++)
        for (unsigned int i=0;i<sizeof(arrivals)/sizeof(arrivals[0]);i++)
            for (unsigned int when=0;when<2;when++)
                check(&d,status,arrivals[i]&~status,when,status%4,false);
    printf("PASS: %u native DSI IRQ cases, deferred completions and 192 MT8183 regressions\n",cases);
    puts("MMIO and wakeups modeled; native IRQ returns after one status read even with BUSY asserted.");
}
'''


def build_harness(source):
    s = source.read_text()
    def block(name):
        match = re.search(r'^.*\b'+re.escape(name)+r'(?:\(|\s*\{|\s*=)', s, re.M)
        assert match, name
        start = match.start(); end = s.index('\n}', start)+2
        if s[end:end+1] == ';': end += 1
        return s[start:end]+'\n'
    macros = s[s.index('#define DSI_START'):s.index('struct mtk_phy_timing')]
    code = PRELUDE+macros+block('mtk_dsi_driver_data')+MODEL
    for name in ('mtk_dsi_mask', 'mtk_dsi_irq_data_set', 'mtk_dsi_irq',
                 'mt6765_dsi_driver_data', 'mt8183_dsi_driver_data'):
        code += block(name)
    path = ROOT/'out/dsi-irq-host.c'; path.write_text(code+MAIN)
    binary = ROOT/'out/dsi-irq-host'
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-fno-pie', '-no-pie', '-O1', '-g',
                    str(path), '-o', str(binary)], check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
        default=ROOT/'src/mainline/drivers/gpu/drm/mediatek/mtk_dsi.c')
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')
    subprocess.run([str(build_harness(args.source))], check=True)
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    statuses = sorted({0, 3, 0xb, 0x1234, 0xffff, *(1<<i for i in range(16))})
    fixtures = [stock_irq(raw, status|busy, late & ~status, cmdq)
                for status in statuses for busy in (0, 0x80000000)
                for late in (0, 0xb, 0xffff) for cmdq in (0, 1)]
    (ROOT/'out/dsi-irq-audit.json').write_text(json.dumps(dict(
        stock_function='disp_irq_handler', raw_offset='0x742260', fixtures=fixtures,
        native_owned_flags='0x000b', hardware_tested=False), indent=2)+'\n')
    print(f'PASS: {len(fixtures)} shipped DSI IRQ executions, CPU/CMDQ RD_RDY policy and late events')


if __name__ == '__main__': main()
