#!/usr/bin/env python3
import argparse
import ast
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

HERE = Path(__file__).resolve().parent
ROOT = Path('/rabbitr1')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parser = argparse.ArgumentParser(description='Check sparse conversion without device access')
parser.add_argument('--avbtool', type=Path, help='Optional pinned AOSP sparse-reader comparison')
parser.add_argument('--mtkclient-xflash', type=Path, help='Optional pinned actual writeflash replay')
parser.add_argument('--output', type=Path, default=ROOT/'out/android-sparse-tests')
args, remaining = parser.parse_known_args()
OUT = args.output.resolve()
if not OUT.is_relative_to(ROOT):
    parser.error('Output must stay under /rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
subject = load('r1_sparse', HERE / 'expand-android-sparse.py')

sha = lambda data: hashlib.sha256(data).hexdigest()

for option, expected in [(args.avbtool, 'aa080bb3dd613cbbcaea410a8cff62225811fc79a4af05613a63ec3acec7ab92'),
                         (args.mtkclient_xflash, 'cd55b262d1acfbda33833aabf3669997cb81d1b58b3c1844d4b4bef71f446b7b')]:
    if option is not None:
        if not option.resolve().is_relative_to(ROOT) or sha(option.read_bytes()) != expected:
            parser.error('External test source differs from its reviewed pin: ' + str(option))
avb = load('aosp_avbtool', args.avbtool) if args.avbtool is not None else None


def fixture(extended=False):
    block = 4096
    raw = bytes(range(256)) * 32
    parts = [(0xCAC1, 2, raw), (0xCAC4, 0, struct.pack('<I', zlib.crc32(raw))),
             (0xCAC2, 3, b'\x12\x34\xab\xcd'), (0xCAC3, 7, b'')]
    expected = raw + b'\x12\x34\xab\xcd' * (3 * block // 4) + bytes(7 * block)
    parts += [(0xCAC2, 1, bytes(4)), (0xCAC1, 1, bytes(block))]
    expected += bytes(2 * block)
    parts += [(0xCAC4, 0, struct.pack('<I', zlib.crc32(expected)))]
    extra = b'extra-v1' if extended else b''
    result = struct.pack('<IHHHHIIII', 0xED26FF3A, 1, int(extended),
                         28 + len(extra), 12 + len(extra), block,
                         len(expected) // block, len(parts), zlib.crc32(expected)) + extra
    offsets = []
    for kind, blocks, payload in parts:
        offsets.append(len(result))
        result += struct.pack('<HHII', kind, 0, blocks, 12 + len(extra) + len(payload)) + extra + payload
    return result, expected, offsets


class SparseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cases-', dir=OUT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source, self.output = self.folder/'input.img', self.folder/'raw.img'
        self.data, self.raw, self.offsets = fixture()

    def convert(self, data=None, **kwargs):
        data = self.data if data is None else data
        self.source.write_bytes(data)
        return subject.expand(self.source, expected_size=len(self.raw),
                              expected_sha256=kwargs.pop('expected_sha256', sha(data)),
                              output=self.output, **kwargs)

    def rejected(self, data):
        with self.assertRaises((ValueError, OSError)):
            self.convert(data)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.r1-raw-*')), [])

    def test_aosp_and_raw_bytes_agree(self):
        report = self.convert()
        self.assertEqual(self.output.read_bytes(), self.raw)
        self.assertEqual(report['raw_sha256'], sha(self.raw))
        self.assertEqual(report['raw_crc32'], f'{zlib.crc32(self.raw):08x}')
        if avb is not None:
            oracle = avb.ImageHandler(str(self.source), read_only=True)
            try:
                self.assertEqual(oracle.image_size, len(self.raw))
                self.assertEqual(oracle.read(len(self.raw)), self.raw)
            finally:
                oracle._image.close()
        self.assertLess(self.output.stat().st_blocks*512, self.output.stat().st_size)

    def test_extended_headers_and_minor_version(self):
        data, raw, _ = fixture(True)
        report = self.convert(data)
        self.assertEqual(self.output.read_bytes(), raw)
        self.assertEqual(report['minor_version'], 1)

    def test_header_corruption(self):
        for offset, fmt, value in [(0,'I',0), (4,'H',2), (8,'H',27), (10,'H',11),
                                   (12,'I',0), (12,'I',4095), (16,'I',15),
                                   (20,'I',0xffffffff), (24,'I',1)]:
            with self.subTest(offset=offset, value=value):
                data = bytearray(self.data);struct.pack_into('<'+fmt, data, offset, value)
                self.rejected(bytes(data))

    def test_chunk_corruption(self):
        first, crc, fill, skip, _, _, last_crc = self.offsets
        for base, offset, fmt, value in [(first,0,'H',0), (first,2,'H',1),
                (first,4,'I',100), (first,8,'I',11), (first,8,'I',14),
                (fill,8,'I',20), (skip,8,'I',13), (crc,4,'I',1),
                (crc,8,'I',15), (last_crc,12,'I',0)]:
            with self.subTest(base=base, offset=offset, value=value):
                data = bytearray(self.data);struct.pack_into('<'+fmt,data,base+offset,value)
                self.rejected(bytes(data))

    def test_truncation_and_trailing_bytes(self):
        for boundary in sorted({0, 1, 27, 28, len(self.data)-1,
                                *self.offsets, *(o+11 for o in self.offsets)}):
            with self.subTest(boundary=boundary):self.rejected(self.data[:boundary])
        self.rejected(self.data+b'\0')

    def test_wrong_pin_and_existing_output(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.convert(expected_sha256='0'*64)
        self.assertFalse(self.output.exists())
        self.output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.convert()
        self.assertEqual(self.output.read_bytes(), b'keep')

    def test_symlinks_and_nonregular_input(self):
        actual=self.folder/'actual';actual.write_bytes(self.data)
        self.source.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            subject.expand(self.source,expected_size=len(self.raw),expected_sha256=sha(self.data))
        self.source.unlink();os.mkfifo(self.source)
        with self.assertRaisesRegex(ValueError, 'regular'):
            subject.expand(self.source,expected_size=len(self.raw),expected_sha256=sha(self.data))
        self.source.unlink();self.output.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'Symlink'):self.convert()
        self.assertEqual(actual.read_bytes(),self.data)

    def test_sync_failure_and_publish_race_preserve_output(self):
        with patch.object(subject.os,'fsync',side_effect=OSError(errno.ENOSPC,'injected')):
            self.rejected(self.data)
        link=os.link
        def race(src,dst,**kwargs):
            fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,
                       dir_fd=kwargs['dst_dir_fd'])
            with os.fdopen(fd,'wb') as stream:stream.write(b'preserve racing creator')
            return link(src,dst,**kwargs)
        with patch.object(subject.os,'link',side_effect=race):
            with self.assertRaises(FileExistsError):self.convert()
        self.assertEqual(self.output.read_bytes(),b'preserve racing creator')
        self.assertEqual(list(self.folder.glob('.r1-raw-*')),[])

    def test_flush_and_close_failures_still_remove_temporary(self):
        fdopen=os.fdopen
        class BrokenSink:
            def __init__(self,stream):self.stream=stream
            def __getattr__(self,name):return getattr(self.stream,name)
            def flush(self):raise OSError(errno.ENOSPC,'injected buffered flush')
            def close(self):
                self.stream.close()
                raise OSError(errno.ENOSPC,'injected close flush')
        def wrapped(fd,mode,*args,**kwargs):
            stream=fdopen(fd,mode,*args,**kwargs)
            return BrokenSink(stream) if mode=='w+b' else stream
        with patch.object(subject.os,'fdopen',side_effect=wrapped):
            self.rejected(self.data)

    def test_output_parent_substitution_never_follows_new_symlink(self):
        parent=self.folder/'destination';parent.mkdir()
        saved=self.folder/'original-destination'
        alternate=self.folder/'alternate';alternate.mkdir()
        self.output=parent/'raw.img'
        create=subject.create_temporary
        def substitute(fd):
            parent.rename(saved);parent.symlink_to(alternate,target_is_directory=True)
            return create(fd)
        with patch.object(subject,'create_temporary',side_effect=substitute):
            with self.assertRaisesRegex(ValueError,'Parent directory changed'):
                self.convert()
        self.assertEqual(list(alternate.iterdir()),[])
        self.assertEqual(list(saved.iterdir()),[])

    def test_source_parent_substitution_reads_only_pinned_directory(self):
        parent=self.folder/'source';parent.mkdir()
        saved=self.folder/'original-source'
        alternate=self.folder/'alternate';alternate.mkdir()
        self.source=parent/'input.img'
        self.source.write_bytes(self.data)
        original_inode=self.source.stat().st_ino
        (alternate/'input.img').write_bytes(b'not the pinned source')
        open_file=os.open;seen=[]
        def substitute(path,flags,*args,**kwargs):
            if path=='input.img' and 'dir_fd' in kwargs:
                parent.rename(saved);parent.symlink_to(alternate,target_is_directory=True)
                fd=open_file(path,flags,*args,**kwargs)
                seen.append(os.fstat(fd).st_ino)
                return fd
            return open_file(path,flags,*args,**kwargs)
        with patch.object(subject.os,'open',side_effect=substitute):
            with self.assertRaisesRegex(ValueError,'Parent directory changed'):
                subject.expand(self.source,expected_size=len(self.raw),
                               expected_sha256=sha(self.data),output=self.output)
        self.assertEqual(seen,[original_inode])
        self.assertEqual((alternate/'input.img').read_bytes(),b'not the pinned source')
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.r1-raw-*')),[])

    def test_source_mutation_or_replacement_is_rejected(self):
        fstat=os.fstat
        for replace in (False,True):
            with self.subTest(replace=replace):
                self.source.write_bytes(self.data)
                inode=self.source.stat().st_ino
                calls=0
                def mutate(fd):
                    nonlocal calls
                    before=fstat(fd)
                    if before.st_ino==inode:
                        calls+=1
                        if calls==2:
                            if replace:
                                other=self.folder/'replacement'
                                other.write_bytes(b'changed');os.replace(other,self.source)
                            else:
                                changed=bytearray(self.data);changed[-1]^=1
                                self.source.write_bytes(changed)
                    return fstat(fd)
                with patch.object(subject.os,'fstat',side_effect=mutate):
                    with self.assertRaisesRegex(ValueError,'input changed'):
                        subject.expand(self.source,expected_size=len(self.raw),
                                       expected_sha256=sha(self.data),output=self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.folder.glob('.r1-raw-*')),[])

    @unittest.skipUnless(args.mtkclient_xflash is not None, 'Optional pinned mtkclient source not supplied')
    def test_actual_mtkclient_writeflash_is_raw(self):
        self.convert()
        path=args.mtkclient_xflash
        source=path.read_text();nodes=[n for n in ast.walk(ast.parse(source))
                                      if isinstance(n,ast.FunctionDef) and n.name=='writeflash']
        self.assertEqual(len(nodes),1)
        code=textwrap.dedent(ast.get_source_segment(source,nodes[0]))
        env={'os':os,'pack':struct.pack,'progress':lambda **kw:SimpleNamespace()}
        exec(compile(code,str(path),'exec'),env)
        observations=[]
        for file,wanted in [(self.source,self.data),(self.output,self.raw)]:
            payload=[];requests=[]
            fake=SimpleNamespace(
                daconfig=SimpleNamespace(storage=SimpleNamespace(get_storage=lambda p,n:(1,p,n))),
                mtk=SimpleNamespace(config=SimpleNamespace(guiprogress=None)),
                get_packet_length=lambda:SimpleNamespace(write_packet_length=4096),
                cmd_write_data=lambda *args:requests.append(args) or True,
                send_param=lambda fields:payload.append(bytes(fields[2])) or True,
                status=lambda:0,send_devctrl=lambda *args:None,
                cmd=SimpleNamespace(CC_OPTIONAL_DOWNLOAD_ACT=1),error=self.fail)
            self.assertTrue(env['writeflash'](fake,0,len(self.raw),str(file),display=False))
            expected=wanted+bytes((-len(wanted))%512)
            self.assertEqual(b''.join(payload),expected)
            self.assertEqual(requests[0][1],len(expected))
            observations.append({'input':file.name,'requested_bytes':len(expected),
                                 'sent_sha256':sha(expected),'sparse_magic_sent':expected[:4].hex()})
        self.assertNotEqual(observations[0]['requested_bytes'],len(self.raw))
        (OUT/'mtkclient-raw-write-replay.json').write_text(json.dumps({
            'source_sha256':sha(path.read_bytes()),'method_sha256':sha(code.encode()),
            'observations':observations,'hardware_accessed':False,
            'scope':'Actual xflash writeflash with mocked storage/USB/progress boundaries'},indent=2)+'\n')


if __name__=='__main__':
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
