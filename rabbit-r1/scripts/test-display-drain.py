#!/usr/bin/env python3
"""Check that native RDMA/OVL teardown retains resources until reset completes."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path('/rabbitr1')
DRIVERS = ROOT/'src/mainline/drivers/gpu/drm/mediatek'


def load(kind, source):
    spec = importlib.util.spec_from_file_location(kind, Path(__file__).with_name(f'test-mt6765-{kind}.py'))
    module = importlib.util.module_from_spec(spec)
    argv = sys.argv
    try:
        sys.argv = [argv[0]]
        spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    module.args.source = source
    return module


MAIN = r'''
#include <setjmp.h>
static jmp_buf blocked;
static struct PRIV priv;
static struct device dev;
static struct clk owned_clock;
static unsigned int retries, recover_after;
static bool permanent, buffers_owned, native;

static void retry(void)
{
    assert(clock_refs==1 && irq_depth==1 && !irq_inflight);
    assert(buffers_owned && !priv.config_valid && OWNED());
    assert(!(load(ENGINE)&1) && !load(INTEN) && !load(INTSTA));
    assert(!(load(RESET)&RESET_BIT)); /* Even a stage-one timeout releases reset. */
    retries++;
    if (permanent && retries==3)
        longjmp(blocked, 1); /* Observe a blocked teardown without hanging the test. */
    if (retries==recover_after)
        reset_error=0;
}

int main(int argc, char **argv)
{
    assert(argc==7);
    reset_error=atoi(argv[1]);
    int attempts=atoi(argv[2]);
    permanent=attempts<0; recover_after=permanent?0:(unsigned int)attempts;
    latency=atoi(argv[3]);
    u32 seed=strtoul(argv[4],NULL,0);
    unsigned int variant=atoi(argv[5]);
    native=atoi(argv[6]);
    priv=(struct PRIV){.clk=&owned_clock, .regs=regs, .irq=73,
                       .config_valid=true, .dev=&dev};
    dev.data=&priv;
    SET_DATA();
    SET_OWNED();
    for (unsigned int off=0;off<sizeof(regs);off+=4) save(off,seed);
    SET_STATE();
    clock_refs=1; irq_depth=0; irq_inflight=native;
    buffers_owned=true; retry_hook=retry;
    if (setjmp(blocked)) {
        assert(permanent && retries==3 && buffers_owned);
        assert(clock_refs==1 && irq_depth==1 && OWNED() && !priv.config_valid);
        puts("held");
        return 0;
    }
    CLK_DISABLE(&dev);
    assert(!permanent && !clock_refs && !OWNED());
    if (native) {
        assert(!priv.config_valid && irq_depth==1 && !irq_inflight && IDLE());
        assert(retries==recover_after && polls);
        assert(!(load(ENGINE)&1) && !load(INTEN) && !load(INTSTA));
    } else {
        assert(!reads && !writes && !polls && !retries && irq_depth==0);
    }
    /* Models the caller releasing buffers only after synchronous teardown. */
    buffers_owned=false;
    puts("released");
    return 0;
}
'''


def harness(kind, shared):
    program = shared.harness()
    assert program.endswith(shared.MAIN)
    program = program[:-len(shared.MAIN)]
    if kind == 'rdma':
        macros = r'''
#define PRIV mtk_disp_rdma
#define CLK_DISABLE mtk_rdma_clk_disable
#define ENGINE 0x10
#define INTEN 0
#define INTSTA 4
#define RESET 0x10
#define RESET_BIT 16
#define OWNED() (priv.clock_rate!=0)
#define SET_OWNED() (priv.clock_rate=230000000)
#define SET_DATA() (priv.data=native?&mt6765_rdma_driver_data:&mt8183_rdma_driver_data)
#define SET_STATE() do { save(0x10,(seed&~0x713U)|0x101|(variant?2:0)); pending=-1; } while (0)
#define IDLE() ((load(0x10)&0x711)==0x100)
'''
    else:
        macros = r'''
#define PRIV mtk_disp_ovl
#define CLK_DISABLE mtk_ovl_clk_disable
#define ENGINE 0xc
#define INTEN 4
#define INTSTA 8
#define RESET 0x14
#define RESET_BIT 1
#define OWNED() (native?priv.clock_enabled:clock_refs!=0)
#define SET_OWNED() (priv.clock_enabled=true)
#define SET_DATA() (priv.data=!native?&mt8192_ovl_driver_data:variant?&mt6765_ovl_2l_driver_data:&mt6765_ovl_driver_data)
#define SET_STATE() do { save(0xc,seed|1); save(0x14,0); save(0x240,0); remaining=-1; } while (0)
#define IDLE() ((load(0x240)&3) && !load(0x14))
'''
    return program+macros+MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for kind in ('rdma', 'ovl'):
        parser.add_argument(f'--{kind}-source', type=Path, default=DRIVERS/f'mtk_disp_{kind}.c')
    args = parser.parse_args()
    folder = ROOT/'out/display-drain-test'
    folder.mkdir(exist_ok=True)
    cases = []
    for kind in ('rdma', 'ovl'):
        source = getattr(args, kind+'_source').resolve()
        if not source.is_relative_to(ROOT):
            raise SystemExit('Sources must be under /rabbitr1')
        shared = load(kind, source)
        code, binary = folder/f'{kind}.c', folder/kind
        code.write_text(harness(kind, shared))
        subprocess.run(['cc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-variable',
                        '-Wno-unused-const-variable', '-fsanitize=address,undefined',
                        '-fno-pie', '-no-pie', str(code), '-o', str(binary)], check=True)
        for failure in range(3 if kind == 'rdma' else 2):
            for attempts in (1, 3, -1) if failure else (0,):
                for latency in (0, 3):
                    for variant in (0, 1):
                        values = [failure, attempts, latency, 0xa5a55a5a, variant, 1]
                        result = subprocess.check_output([str(binary), *map(str, values)], text=True).strip()
                        assert result == ('held' if attempts < 0 else 'released')
                        cases.append(dict(driver=kind, failure=failure, attempts=attempts,
                                          latency=latency, variant=variant, result=result))
        assert subprocess.check_output([str(binary), '0', '0', '0', '0', '0', '0'], text=True) == 'released\n'
    report = {'cases': cases, 'hardware_tested': False,
              'scope': 'production reset/clock-disable paths; modeled MMIO, clocks, IRQ drain, retry sleeps and caller buffer ownership'}
    (ROOT/'out/display-drain-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {len(cases)} native teardown cases; delayed completion, both RDMA reset failures, retries and blocked permanent failures')
    print('PASS: clock/IRQ/buffer ownership retained during retries; legacy MT8183/MT8192 teardown unchanged')
    print('No physical DMA completion or LK handoff is established.')


if __name__ == '__main__':
    main()
