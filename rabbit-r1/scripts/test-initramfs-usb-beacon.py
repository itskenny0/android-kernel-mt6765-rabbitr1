#!/usr/bin/env python3
"""Check actual one-shot USB beacon shell logic using private fake filesystem paths."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / 'initramfs/init'
STUBS = r'''
mount() {
    mkdir -p "$FAKE/sys/kernel/config/usb_gadget/r1"
    [ "$CASE" != bind_failure ] || mkdir "$FAKE/sys/kernel/config/usb_gadget/r1/UDC"
}
printf() {
    command printf "$@"
    if [ "$1" = '%s\n' ] && [ "${2:-}" = controller0 ]; then
        command printf 'bind %s\n' "$ticks" >> "$FAKE/transitions"
    fi
    if [ "$1" = '\n' ]; then
        unbound=1
        command printf 'unbind %s\n' "$ticks" >> "$FAKE/transitions"
    fi
    if [ "$1" = '<6>r1: USB ACM %s\n' ]; then
        command printf '%s\n' "$2" >> "$FAKE/events"
    fi
}
read() {
    if [ "$#" = 2 ] && [ "$2" = bound ]; then
        if [ "$unbound" = 1 ]; then
            case "$CASE" in
              unbind_read_failure) return 1 ;;
              unbind_read_mismatch) bound=controller0; return 0 ;;
            esac
        elif [ "$CASE" = bind_read_mismatch ]; then
            bound=other; return 0
        fi
    fi
    command read "$@"
}
sleep() {
    [ "$1" != 2 ] || exit 0
    command printf '%s\n' "$1" >> "$FAKE/waits"
    [ "$CASE:$1" != pre_wait_failure:20 ] || return 1
    [ "$CASE:$1" != bound_wait_failure:10 ] || return 1
    ticks=$((ticks + $1))
    command printf '%s.00 0.00\n' "$ticks" > "$FAKE/proc/uptime"
    if [ "$CASE" = delayed ] && [ "$ticks" = 2 ]; then
        mkdir "$FAKE/sys/class/udc/controller0"
    fi
    if [ "$CASE:$1" = unbind_write_failure:10 ]; then
        mv "$FAKE/sys/kernel/config/usb_gadget/r1/UDC" "$FAKE/old-UDC"
        mkdir "$FAKE/sys/kernel/config/usb_gadget/r1/UDC"
    fi
}
getty() { command printf '%s\n' "$*" >> "$FAKE/getty"; }
setsid() { command printf 'uart\n' >> "$FAKE/events"; exit 0; }
ticks=0
unbound=0
'''


def one(shell, root, source, case, status, waits, transitions, rc):
    fake = root / case
    for part in ['sys/class/udc', 'run', 'dev', 'proc']:
        (fake / part).mkdir(parents=True, exist_ok=True)
    (fake / 'dev/kmsg').touch()
    (fake / 'proc/uptime').write_text('0.00 0.00\n')
    cmdline = 'r1.usb=1 r1.usb_beacon=1\n'
    if case == 'not_opted_in': cmdline = 'r1.usb=1 r1.usb_beacon=10\n'
    if case == 'disabled': cmdline = 'r1.usb=0 r1.usb_beacon=1\n'
    (fake / 'proc/cmdline').write_text(cmdline)
    if case != 'delayed': (fake / 'sys/class/udc/controller0').mkdir()
    body = source[source.index('usb_status()\n'):]
    for namespace in ['/sys/', '/run/', '/dev/kmsg', '/proc/']:
        body = body.replace(namespace, str(fake) + namespace)
    if case != 'disabled': body = body[:body.index('# Explicit opt-in.')] + 'usb_start\n'
    script = fake / 'run.sh'
    script.write_text('FAKE=' + shlex.quote(str(fake)) + '\nCASE=' + shlex.quote(case) + '\n' + STUBS + body)
    run = subprocess.run([*shell, str(script)], capture_output=True, text=True, timeout=5)
    assert run.returncode == rc, (case, run.stdout, run.stderr)
    assert (fake / 'run/usb-start.status').read_text().strip() == status, case
    actual_waits = [int(x) for x in (fake/'waits').read_text().splitlines()] if (fake/'waits').exists() else []
    actual_transitions = (fake/'transitions').read_text().splitlines() if (fake/'transitions').exists() else []
    assert actual_waits == waits and actual_transitions == transitions, (case, actual_waits, actual_transitions)
    assert (fake/'getty').exists() == (case == 'not_opted_in'), case
    if case not in ('not_opted_in', 'disabled'):
        log = (fake/'run/usb-beacon.log').read_text()
        assert 'uptime=0.00 USB beacon: setup-started\n' in log
        assert log.endswith('USB beacon: ' + status + '\n')
        assert all(line.startswith('uptime=') for line in log.splitlines())
    else:
        assert not (fake/'run/usb-beacon.log').exists()
    if case in ('success', 'delayed'):
        assert (fake/'sys/kernel/config/usb_gadget/r1/UDC').read_text() == '\n'
        assert (fake/'events').read_text().count('beacon complete: unbound; no rebind') == 1
    if case == 'disabled': assert (fake/'events').read_text().splitlines() == ['disabled by boot profile', 'uart']
    return {'case':case, 'status':status, 'waits':waits, 'transitions':transitions, 'exit':rc}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);source=SOURCE.read_text()
    # Reuse all original controls without modifying their source/assertions.
    spec=importlib.util.spec_from_file_location('normal_usb_tests',HERE/'test-initramfs-usb.py')
    normal=importlib.util.module_from_spec(spec);spec.loader.exec_module(normal)
    ordinary=[('immediate',0,'bound: controller0'),('delayed',2,'bound: controller0'),('deadline',30,'bound: controller0'),('absent',-1,'failed: UDC discovery timeout'),('mount_failure',0,'failed: configfs mount'),('setup_failure',0,'failed: gadget setup'),('bind_failure',0,'failed: bind controller0'),('read_failure',0,'failed: bind readback controller0'),('read_mismatch',0,'failed: bind readback controller0'),('sleep_failure',-1,'failed: UDC discovery wait'),('disabled',0,'disabled by boot profile'),('background',-1,'failed: UDC discovery timeout')]
    cases=[('success','beacon complete: unbound; no rebind',[20,10],['bind 20','unbind 30'],0),('delayed','beacon complete: unbound; no rebind',[1,1,20,10],['bind 22','unbind 32'],0),('pre_wait_failure','failed: beacon pre-bind wait',[20],[],1),('bound_wait_failure','failed: beacon bound wait; no unbind requested',[20,10],['bind 20'],1),('bind_failure','failed: bind controller0',[20],[],1),('bind_read_mismatch','failed: bind readback controller0',[20],['bind 20'],1),('unbind_write_failure','failed: beacon unbind controller0',[20,10],['bind 20'],1),('unbind_read_failure','failed: beacon unbind readback controller0',[20,10],['bind 20','unbind 30'],1),('unbind_read_mismatch','failed: beacon unbind readback controller0',[20,10],['bind 20','unbind 30'],1),('not_opted_in','bound: controller0',[],['bind 0'],0),('disabled','disabled by boot profile',[],[],0)]
    shells=[['/bin/sh']]
    if shutil.which('busybox'):shells.append([shutil.which('busybox'),'sh'])
    results=[]
    for i,shell in enumerate(shells):
        subprocess.run([*shell,'-n',str(SOURCE)],check=True)
        with tempfile.TemporaryDirectory(prefix=f'shell-{i}-',dir=out) as temp:
            root=Path(temp);(root/'normal').mkdir();(root/'beacon').mkdir()
            old=[normal.run_case(shell,root/'normal',source,*case) for case in ordinary]
            new=[one(shell,root/'beacon',source,*case) for case in cases]
        results.append({'shell':shell,'unchanged_normal_controls':old,'beacon_controls':new})
    record={'passed':True,'normal_controls_per_shell':len(ordinary),'beacon_controls_per_shell':len(cases),'shells':results,'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'scope':'Actual shell body, fake clock/configfs/UDC/mount/getty. No kernel callbacks, USB, physical timing or disconnect guarantee.'}
    (out/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps({'passed':True,'normal_controls_per_shell':len(ordinary),'beacon_controls_per_shell':len(cases),'shells':len(shells)}))

if __name__=='__main__':main()
