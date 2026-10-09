#!/usr/bin/env python3
"""Compile and run the expdb checkpoint parser and mocked syscall tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess


WORKSPACE = Path('/rabbitr1')


def confined(value):
    path = Path(value).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise argparse.ArgumentTypeError('Paths must stay under /rabbitr1')
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=confined,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out', type=confined,
                        default=WORKSPACE / 'out/expdb-checkpoint-tests')
    args = parser.parse_args()
    root = confined(args.source_root)
    output = confined(args.out)
    output.mkdir(parents=True, exist_ok=True)
    temp = output / 'tmp'
    temp.mkdir(exist_ok=True)
    source = root / 'initramfs/expdb-checkpoint.c'
    tests = root / 'tests/expdb-checkpoint.c'
    header = root / 'android/device/logging/ExpdbLayout.h'
    inputs = [source, tests, header, Path(__file__).resolve()]
    for path in inputs:
        confined(path)
        if not path.is_file():
            raise SystemExit(f'Missing test input: {path}')
    env = dict(os.environ, TMPDIR=str(temp),
               ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',
               UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    binary = output / 'expdb-checkpoint-test'
    command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
               '-fno-pie', '-no-pie', '-fsanitize=address,undefined',
               '-I', str(root / 'initramfs'), '-o', str(binary), str(tests)]
    result = {'schema': 1, 'state': 'running', 'compile_command': command,
              'input_sha256': {str(path): digest(path) for path in inputs},
              'scope': 'Host parser and mocked syscall boundaries; no hardware validation'}
    report = output / 'result.json'
    report.write_text(json.dumps(result, indent=2) + '\n')
    try:
        compiled = subprocess.run(command, env=env, text=True,
                                  capture_output=True, timeout=60)
        (output / 'compile.stdout').write_text(compiled.stdout)
        (output / 'compile.stderr').write_text(compiled.stderr)
        compiled.check_returncode()
        tested = subprocess.run([str(binary)], env=env, text=True,
                                capture_output=True, timeout=30)
        (output / 'test.stdout').write_text(tested.stdout)
        (output / 'test.stderr').write_text(tested.stderr)
        tested.check_returncode()
        if not any(line.startswith('PASS: ') for line in tested.stdout.splitlines()):
            raise RuntimeError('Missing test completion record')
        result.update(state='passed', binary_sha256=digest(binary),
                      summary=tested.stdout.strip().splitlines()[-1])
    except Exception as error:
        result.update(state='failed', error=str(error))
        raise
    finally:
        report.write_text(json.dumps(result, indent=2) + '\n')
    print(result['summary'])


if __name__ == '__main__':
    main()
