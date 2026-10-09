#!/usr/bin/env python3
"""Run actual pstore callbacks and credential scopes with mocked kernel I/O."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
KERNEL = ROOT / 'src/mainline'
OUT = ROOT / 'out/pstore-credentials'


def function(source, name):
    match = re.search(r'static (?:inline )?[\w *]+\b' + re.escape(name) +
                      r'\([^;]*?\)\n\{', source)
    if not match:
        raise ValueError('Missing production function: ' + name)
    return source[match.start():source.index('\n}', match.end()) + 2] + '\n'


def macro(source, name):
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(r'#define\s+' + re.escape(name) + r'\(', line):
            end = index
            while lines[end].rstrip().endswith('\\'):
                end += 1
            return ''.join(lines[index:end + 1]) + '\n'
    raise ValueError('Missing production macro: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', type=Path, default=KERNEL / 'fs/pstore/blk.c')
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must stay under /rabbitr1')
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ['TMPDIR'] = str(ROOT / '.tmp')
    os.environ['ASAN_OPTIONS'] = 'detect_leaks=1:abort_on_error=1'
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    blk = source.read_text()
    cred = (KERNEL / 'include/linux/cred.h').read_text()
    cleanup = (KERNEL / 'include/linux/cleanup.h').read_text()
    prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>

struct cred { unsigned int identity; };
struct file { const struct cred *f_cred; };
struct task_struct { const struct cred *cred; };
static struct task_struct task;
static struct task_struct *current = &task;
static struct file backing;
static struct file *psblk_file = &backing;
static const struct cred *exchanges[4];
static unsigned int exchange_count, io_count, checks;
static bool interrupt_context, disabled_irqs, expect_write;
static ssize_t io_result;
static void *expected_buffer;
static size_t expected_bytes;
static loff_t expected_pos;

/* Substitute only the RCU pointer operation; the scope and credential
 * functions below are extracted from the kernel, including their cleanup. */
static const struct cred *replace_cred(const struct cred **slot,
                                      const struct cred *next, int condition)
{
    const struct cred *old = *slot;
    assert(slot == &current->cred && condition == 1);
    assert(exchange_count < sizeof(exchanges) / sizeof(exchanges[0]));
    exchanges[exchange_count++] = next;
    *slot = next;
    return old;
}
#define rcu_replace_pointer(slot, next, condition) \
    replace_cred(&(slot), (next), (condition))
#define __cleanup(fn) __attribute__((cleanup(fn)))
#define __no_context_analysis
#define JOIN_IMPL(a, b) a##b
#define JOIN(a, b) JOIN_IMPL(a, b)
#define __UNIQUE_ID(prefix) JOIN(prefix, __COUNTER__)

static bool in_interrupt(void) { return interrupt_context; }
static bool irqs_disabled(void) { return disabled_irqs; }

static ssize_t transfer(struct file *file, const void *buf, size_t bytes,
                        loff_t *pos, bool write)
{
    assert(file == psblk_file && current->cred == file->f_cred);
    assert(exchange_count == 1 && exchanges[0] == file->f_cred);
    assert(buf == expected_buffer && bytes == expected_bytes);
    assert(*pos == expected_pos && write == expect_write);
    assert(!io_count++);
    if (io_result > 0) {
        assert((size_t)io_result <= bytes);
        if (!write)
            memset((void *)buf, 0xa5, (size_t)io_result);
        *pos += io_result;
    }
    return io_result;
}
static ssize_t kernel_read(struct file *file, void *buf, size_t bytes, loff_t *pos)
{
    return transfer(file, buf, bytes, pos, false);
}
static ssize_t kernel_write(struct file *file, const void *buf, size_t bytes, loff_t *pos)
{
    return transfer(file, buf, bytes, pos, true);
}
'''
    helpers = ''.join(macro(cleanup, name) for name in
                      ('DEFINE_CLASS', 'CLASS', '__scoped_class', 'scoped_class'))
    helpers += function(cred, 'override_creds') + function(cred, 'revert_creds')
    scope = re.search(r'^DEFINE_CLASS\(override_creds,[\s\S]*?\)\n', cred, re.M)
    if not scope:
        raise ValueError('Missing kernel credential cleanup class')
    helpers += scope[0] + '\n' + macro(cred, 'scoped_with_creds')
    callbacks = ''.join(function(blk, name) for name in
                        ('psblk_generic_blk_read', 'psblk_generic_blk_write'))
    tests = r'''
static void check(bool write, const struct cred *owner, size_t bytes,
                  loff_t pos, ssize_t result, unsigned int atomic_flags)
{
    char guarded[4098];
    const struct cred *caller = current->cred;
    ssize_t returned;

    memset(guarded, 0x3c, sizeof(guarded));
    backing.f_cred = owner;
    exchange_count = io_count = 0;
    memset(exchanges, 0, sizeof(exchanges));
    expected_buffer = guarded + 1;
    expected_bytes = bytes;
    expected_pos = pos;
    expect_write = write;
    io_result = result;
    interrupt_context = atomic_flags & 1;
    disabled_irqs = atomic_flags & 2;
    if (write)
        returned = psblk_generic_blk_write(expected_buffer, bytes, pos);
    else
        returned = psblk_generic_blk_read(expected_buffer, bytes, pos);
    assert(current->cred == caller);
    assert(backing.f_cred == owner);
    if (write && atomic_flags) {
        assert(returned == -EBUSY);
        assert(exchange_count == 0 && io_count == 0);
    } else {
        assert(returned == result);
        assert(exchange_count == 2 && io_count == 1);
        assert(exchanges[0] == owner && exchanges[1] == caller);
    }
    for (size_t i = 0; i < sizeof(guarded); ++i) {
        const unsigned char expected = !write && result > 0 &&
                i > 0 && i <= (size_t)result ? 0xa5 : 0x3c;
        assert((unsigned char)guarded[i] == expected);
    }
    ++checks;
}

int main(void)
{
    static const struct cred identities[] = {{1}, {2}, {3}};
    const size_t lengths[] = {0, 1, 4096};
    const loff_t offsets[] = {0, 2048, 18 * 1024 * 1024 - 4096};
    const struct cred *saved;

    for (unsigned int owner = 0; owner < 3; ++owner)
        for (unsigned int caller = 0; caller < 3; ++caller)
            for (unsigned int length = 0; length < 3; ++length)
                for (unsigned int offset = 0; offset < 3; ++offset) {
                    const ssize_t results[] = {
                        (ssize_t)lengths[length], (ssize_t)lengths[length] / 2,
                        0, -EIO, -EACCES, -ENOSPC, -EINTR
                    };
                    current->cred = &identities[caller];
                    for (unsigned int result = 0; result < 7; ++result)
                        for (unsigned int write = 0; write < 2; ++write)
                            check(write, &identities[owner], lengths[length],
                                  offsets[offset], results[result], 0);
                    for (unsigned int atomic_flags = 1; atomic_flags < 4; ++atomic_flags)
                        check(true, &identities[owner], lengths[length], offsets[offset],
                              lengths[length], atomic_flags);
                }

    /* Preserve an existing override, then let its caller restore its own SID. */
    for (unsigned int write = 0; write < 2; ++write) {
        current->cred = &identities[0];
        exchange_count = 0;
        saved = override_creds(&identities[1]);
        check(write, &identities[2], 4096, 2048, -EIO, 0);
        assert(current->cred == &identities[1]);
        revert_creds(saved);
        assert(current->cred == &identities[0]);
    }
    printf("PASS: %u pstore credential cases; real callbacks and cleanup, mocked I/O/RCU\n",
           checks);
    return 0;
}
'''
    generated = OUT / 'host.c'
    generated.write_text(prelude + helpers + callbacks + tests)
    executable = OUT / 'host'
    command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
               '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
               str(generated), '-o', str(executable)]
    subprocess.run(command, check=True)
    subprocess.run([str(executable)], check=True)
    (OUT / 'result.json').write_text(json.dumps({
        'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'checks': 1379, 'status': 'pass',
        'scope': 'Actual callbacks and credential cleanup with mocked kernel I/O and RCU',
        'limits': 'Does not exercise the real LSM, scheduler, storage or device runtime',
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
