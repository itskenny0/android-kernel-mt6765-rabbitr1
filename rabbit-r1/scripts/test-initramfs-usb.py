#!/usr/bin/env python3
"""Run actual init USB shell logic with local fake configfs/UDC paths; no devices."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / 'initramfs/init'
STUBS = r'''
mount() {
    [ "$CASE" != mount_failure ] || return 1
    mkdir -p "$FAKE/sys/kernel/config/usb_gadget/r1"
    case "$CASE" in
        setup_failure) mkdir "$FAKE/sys/kernel/config/usb_gadget/r1/idVendor" ;;
        bind_failure) mkdir "$FAKE/sys/kernel/config/usb_gadget/r1/UDC" ;;
    esac
}
printf() {
    command printf "$@"
    case "$1" in '<6>r1: USB ACM %s\n')
        command printf '%s\n' "$2" >> "$FAKE/events" ;;
    esac
}
read() {
    case "$CASE" in
        read_failure) return 1 ;;
        read_mismatch) bound=other-controller ;;
        *) command read "$@" ;;
    esac
}
sleep() {
    [ "$1" != 2 ] || exit 0
    waits=$((waits + 1))
    command printf '%s\n' "$waits" > "$FAKE/waits"
    [ "$CASE" != sleep_failure ] || return 1
    if [ "$waits" = "$APPEAR" ]; then mkdir "$FAKE/sys/class/udc/controller0"; fi
    if [ "$CASE" = background ]; then /bin/sleep 0.01; fi
}
getty() { command printf '%s\n' "$*" >> "$FAKE/getty"; }
setsid() { command printf 'uart\n' >> "$FAKE/events"; exit 0; }
waits=0
'''


def run_case(shell, root, source, case, appear=0, expect=None):
    fake = root / case
    fake.mkdir()
    for name in ['sys/class/udc', 'run', 'dev', 'proc']:
        (fake / name).mkdir(parents=True, exist_ok=True)
    (fake / 'dev/kmsg').touch()
    (fake / 'proc/cmdline').write_text('r1.usb=0\n' if case == 'disabled' else 'r1.usb=1\n')
    if appear == 0:
        (fake / 'sys/class/udc/controller0').mkdir()
    # Relocate only fixed filesystem namespaces, not the tested shell logic.
    start = source.index('usb_status()\n')
    body = source[start:]
    for namespace in ['/sys/', '/run/', '/dev/kmsg', '/proc/cmdline']:
        body = body.replace(namespace, str(fake) + namespace)
    if case not in ['disabled', 'background']:
        body = body[:body.index('# Explicit opt-in.')] + 'usb_start\n'
    script = fake / 'run.sh'
    script.write_text('FAKE=' + shlex.quote(str(fake)) + '\nCASE=' + shlex.quote(case) +
                      '\nAPPEAR=' + str(appear) + '\n' + STUBS + '\n' + body)
    result = subprocess.run([*shell, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=5)
    status = (fake / 'run/usb-start.status').read_text().strip()
    events = (fake / 'events').read_text().splitlines()
    assert status == expect, (case, result.returncode, result.stdout, status)
    assert (fake / 'dev/kmsg').read_text().strip() == '<6>r1: USB ACM ' + expect
    actual_waits = int((fake / 'waits').read_text()) if (fake / 'waits').exists() else 0
    if expect.startswith('bound:'):
        assert result.returncode == 0, result.stdout
        assert actual_waits == appear, (case, actual_waits)
        assert (fake / 'getty').read_text() == '-L -n -l /bin/sh ttyGS0 115200\n'
        assert (fake / 'sys/kernel/config/usb_gadget/r1/UDC').read_text() == 'controller0\n'
        assert (fake / 'sys/kernel/config/usb_gadget/r1/idVendor').read_text() == '0x1d6b\n'
    else:
        assert not (fake / 'getty').exists(), case
        assert result.returncode == (0 if case in ['disabled', 'background'] else 1), result.stdout
    if case in ['absent', 'background']:
        assert actual_waits == 30
    if case in ['mount_failure', 'setup_failure', 'bind_failure', 'read_failure', 'read_mismatch']:
        assert actual_waits == 0
    if case == 'disabled':
        assert not (fake / 'sys/kernel/config').exists()
        assert events == ['disabled by boot profile', 'uart']
    if case == 'background':
        assert events.index('uart') < events.index('failed: UDC discovery timeout'), events
        assert (fake / 'run/usb-start.log').exists()
    return {'case': case, 'status': status, 'exit': result.returncode,
            'discovery_waits': actual_waits, 'events': events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = SOURCE.read_text()
    # The asynchronous call and UART loop are executed unchanged in the two
    # complete-tail controls, using fake setsid/getty/mount/sleep only.
    assert '(usb_start > /run/usb-start.log 2>&1) &' in source
    shells = [['/bin/sh']]
    busybox = shutil.which('busybox')
    if busybox:
        shells.append([busybox, 'sh'])
    cases = [('immediate', 0, 'bound: controller0'),
             ('delayed', 2, 'bound: controller0'),
             ('deadline', 30, 'bound: controller0'),
             ('absent', -1, 'failed: UDC discovery timeout'),
             ('mount_failure', 0, 'failed: configfs mount'),
             ('setup_failure', 0, 'failed: gadget setup'),
             ('bind_failure', 0, 'failed: bind controller0'),
             ('read_failure', 0, 'failed: bind readback controller0'),
             ('read_mismatch', 0, 'failed: bind readback controller0'),
             ('sleep_failure', -1, 'failed: UDC discovery wait'),
             ('disabled', 0, 'disabled by boot profile'),
             ('background', -1, 'failed: UDC discovery timeout')]
    results = []
    for index, shell in enumerate(shells):
        subprocess.run([*shell, '-n', str(SOURCE)], check=True)
        with tempfile.TemporaryDirectory(prefix=f'shell-{index}-', dir=out) as temp:
            controls = [run_case(shell, Path(temp), source, *case) for case in cases]
        results.append({'shell': shell, 'controls': controls})
    record = {'status': 'pass', 'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'boundary': 'Actual shell logic; fake configfs, UDC, mount, wait, getty and UART; no USB or hardware proof',
              'results': results}
    (out / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'PASS {len(cases)} controls per shell, {len(results)} shells; source ' + record['source_sha256'])


if __name__ == '__main__':
    main()
