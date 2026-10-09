"""Execute patched method bodies with scripted endpoints; never import USB."""
import argparse
import array
import ast
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
from queue import Queue
from struct import pack, unpack
from types import SimpleNamespace as NS
import unittest

ORIGINAL = CANDIDATE = FIXTURES = OUTPUT = None


def configure_paths():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--fixtures', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    workspace = Path('/rabbitr1').resolve(strict=True)
    resolved = {}
    for name in ('original', 'candidate', 'fixtures', 'out'):
        path = getattr(args, name)
        if not path.is_absolute():
            parser.error('--' + name + ' must be an absolute workspace path')
        path = path.resolve(strict=name != 'out')
        if not path.is_relative_to(workspace) or path == workspace:
            parser.error('--' + name + ' must stay below /rabbitr1')
        if name != 'out' and not path.is_dir():
            parser.error('--' + name + ' must be a source/fixture directory')
        resolved[name] = path
    if resolved['out'].exists():
        parser.error('--out must be a new results directory')
    for name in ('original', 'candidate', 'fixtures'):
        if resolved['out'].is_relative_to(resolved[name]):
            parser.error('--out must not be inside an input tree')
    resolved['out'].mkdir(parents=True)
    return tuple(resolved[name] for name in ('original', 'candidate', 'fixtures', 'out'))
REL = 'mtkclient/Library/Connection/usblib.py'
CONTROLS = []


def load(tree='candidate'):
    path = {'original': ORIGINAL, 'candidate': CANDIDATE}[tree] / REL
    parsed = ast.parse(path.read_text())
    kept = [n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name in ('UsbClass', 'UsbTransferError')]
    class USBError(Exception):
        @property
        def strerror(self):return str(self)
    events = []
    usb = NS(core=NS(USBError=USBError, find=lambda **kw: events.append('discover') or []),
             util=NS(dispose_resources=lambda dev: events.append('dispose')))
    clock = NS(monotonic_ns=lambda: 0, sleep=lambda n: events.append(('sleep', n)))
    ns = dict(DeviceClass=object, usb=usb, time=clock, logging=logging, array=array,
              Queue=Queue, pack=pack, unpack=unpack, max_xml_data_length=4096)
    exec(compile(ast.Module(body=kept, type_ignores=[]), str(path), 'exec'), ns)
    cls = ns['UsbClass']; link = object.__new__(cls)
    link._io_failure = None; link._io_active = False; link._io_last_time = None
    link.timeout = 1000; link.maxsize = 512; link.fast = False; link.queue = Queue()
    link.buffer = array.array('B', [0]) * 1048576; link.loglevel = logging.INFO
    link.verify_data = lambda *args: None
    for name in ('debug','info','warning','error'):setattr(link,name,lambda *args:None)
    link.connected = True; link.interface = 0; link.backend = None
    link.device = NS(reset=lambda:events.append('reset'),
                     is_kernel_driver_active=lambda i:False,
                     attach_kernel_driver=lambda i:events.append('attach'))
    return link, ns, events


class Out:
    wMaxPacketSize = 512
    def __init__(self, steps=None):self.steps=list(steps or []);self.calls=[];self.accepted=bytearray()
    def write(self, data, timeout=None):
        self.calls.append((bytes(data), timeout))
        value = self.steps.pop(0) if self.steps else len(data)
        if callable(value):value=value(data,timeout)
        if isinstance(value,BaseException):raise value
        if type(value) is int and 0 <= value <= len(data):self.accepted.extend(data[:value])
        return value


class In:
    wMaxPacketSize = 512
    def __init__(self,steps):self.steps=list(steps);self.calls=[]
    def read(self,size,timeout=None):
        n=size if type(size) is int else len(size);self.calls.append((n,timeout,type(size).__name__))
        value=self.steps.pop(0)
        if callable(value):value=value(size,timeout)
        if isinstance(value,BaseException):raise value
        if type(size) is not int and type(value) in (bytes,bytearray):
            size[:len(value)]=array.array('B',value);return len(value)
        return value


def protocol(link, tree='candidate'):
    path={'original': ORIGINAL, 'candidate': CANDIDATE}[tree]/'mtkclient/Library/DA/xflash/xflash_lib.py'
    node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='DAXFlash')
    node.body=[n for n in node.body if isinstance(n,ast.FunctionDef) and n.name in ('ack','xsend','xread','status','send_param','send_devctrl','readflash','writeflash')]
    node.bases=[];node.keywords=[]
    ns=dict(pack=pack,unpack=unpack,DataType=NS(DT_PROTOCOL_FLOW=1),
            progress=lambda **kw:NS(update=lambda n:None,done=lambda:None))
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    p=ns['DAXFlash']();p.usbwrite=link.usbwrite;p.usbread=link.usbread;p.error=lambda *args:None
    p.mtk=NS(config=NS(chipconfig=NS(dacode=0x6765),guiprogress=None),port=NS(cdc=link))
    p.cmd=NS(MAGIC=0xFEEEEEEF,DEVICE_CTRL=0x10009,CC_OPTIONAL_DOWNLOAD_ACT=0x800005)
    p.data_type=NS(DT_PROTOCOL_FLOW=1)
    p.daconfig=NS(storage=NS(get_storage=lambda part,length:(1,8,length)))
    p.cmd_read_data=lambda **kw:True;p.cmd_write_data=lambda *args:True
    p.get_packet_length=lambda:NS(read_packet_length=1048576,write_packet_length=512)
    return p


def port(link):
    path=CANDIDATE/'mtkclient/Library/Port.py'
    node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='Port')
    node.body=[n for n in node.body if isinstance(n,ast.FunctionDef) and n.name in
               ('echo','handshake','serial_handshake','run_handshake','run_serial_handshake')]
    node.bases=[];node.keywords=[]
    ns=dict(pack=pack)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    p=ns['Port']();p.cdc=link;p.usbwrite=link.usbwrite;p.usbread=link.usbread
    return p


class Tests(unittest.TestCase):
    def note(self,name):CONTROLS.append(name)
    def rejected(self,link,ns,fn):
        with self.assertRaises(ns['UsbTransferError']):fn()
        self.assertIsNotNone(link._io_failure)

    def test_positive_writes_types_zlp_and_large(self):
        for value in (b'ABCD',bytearray(b'ABCD'),'ABCD',array.array('B',b'ABCD'),
                      memoryview(b'ABCD'),[65,66,67,68],(65,66,67,68)):
            link,ns,_=load();out=Out([1,2,1]);link.EP_OUT=out
            self.assertTrue(link.usbwrite(value));self.assertEqual(out.accepted,b'ABCD')
            self.assertEqual([v[0] for v in out.calls],[b'ABCD',b'BCD',b'D'])
            self.assertTrue(all(v[1]==1000 for v in out.calls));self.note('write-type-'+type(value).__name__)
        link,ns,_=load();link.EP_OUT=Out()
        self.assertTrue(link.usbwrite(b''));self.assertEqual(link.EP_OUT.calls,[(b'',1000)]);self.note('intentional-zlp')
        link,ns,_=load();link.EP_OUT=Out();data=b'X'*(1048576+513)
        self.assertTrue(link.usbwrite(data));self.assertEqual(link.EP_OUT.accepted,data)
        self.assertEqual([len(x[0]) for x in link.EP_OUT.calls],[1048576,513]);self.note('write-over-1MiB')

    def test_read_exact_short_fast_and_large(self):
        for fast in (False,True):
            link,ns,_=load();link.fast=fast;link.EP_IN=In([b'AB',b'C',b'DE'])
            self.assertEqual(link.usbread(5),b'ABCDE')
            self.assertEqual([x[0] for x in link.EP_IN.calls],[5,3,2]);self.note('read-fragments-'+str(fast))
            link,ns,_=load();link.fast=fast;link.EP_IN=In([b'AB'])
            self.assertEqual(link.usbread(5,maxtimeout=-1),b'AB');self.note('read-short-'+str(fast))
            link,ns,_=load();link.fast=fast;link.EP_IN=In([b''])
            self.assertEqual(link.usbread(5,maxtimeout=-1),b'');self.note('read-short-zlp-'+str(fast))
            link,ns,_=load();link.fast=fast;link.EP_IN=In([b'A'*1048576,b'B'*17])
            self.assertEqual(link.usbread(1048593,w_max_packet_size=1048593),b'A'*1048576+b'B'*17)
            self.assertEqual([x[0] for x in link.EP_IN.calls],[1048576,17]);self.note('read-large-final-remainder-'+str(fast))
        link,ns,_=load();link.EP_IN=In([])
        self.assertEqual(link.usbread(0),b'');self.assertEqual(link.EP_IN.calls,[]);self.note('read-zero-local')

    def test_invalid_counts_errors_no_later_traffic(self):
        for bad in (0,-1,9,True,False,None,1.0,OSError('uncertain partial write'),KeyboardInterrupt()):
            link,ns,_=load();link.EP_OUT=Out([2,bad]);link.EP_IN=In([])
            with self.assertRaises((ns['UsbTransferError'],KeyboardInterrupt)):link.usbwrite(b'ABCD')
            self.assertEqual(len(link.EP_OUT.calls),2);self.assertEqual(link.EP_OUT.accepted,b'AB')
            for call in (lambda:link.usbwrite(b'next'),lambda:link.usbread(1),lambda:link.connect()):
                self.rejected(link,ns,call)
            self.assertEqual(len(link.EP_OUT.calls),2);self.assertEqual(link.EP_IN.calls,[])
            self.note('invalid-write-'+repr(bad))
        for fast,bad in ((False,b''),(False,b'LONG'),(False,2),(False,True),(False,array.array('H',[1])),
                         (False,OSError('partial read unknown')),(True,0),(True,-1),(True,4),(True,True)):
            link,ns,_=load();link.fast=fast;link.EP_IN=In([bad]);link.EP_OUT=Out()
            self.rejected(link,ns,lambda:link.usbread(3));self.rejected(link,ns,lambda:link.usbwrite(b'next'))
            self.assertEqual(len(link.EP_IN.calls),1);self.assertEqual(link.EP_OUT.calls,[])
            self.note('invalid-read-'+str(fast)+'-'+repr(bad))
        for bad in (1,True,OSError('ZLP failure')):
            link,ns,_=load();link.EP_OUT=Out([bad]);self.rejected(link,ns,lambda:link.usbwrite(b''))
            self.assertEqual(len(link.EP_OUT.calls),1);self.note('invalid-zlp-'+repr(bad))

    def test_budgets_modes_and_callbacks(self):
        link,ns,_=load();link.BULK_MAX_CALLS=2;link.EP_OUT=Out([1,1,1])
        self.rejected(link,ns,lambda:link.usbwrite(b'ABC'));self.assertEqual(len(link.EP_OUT.calls),2);self.note('finite-call-budget')
        for elapsed,success in ((999999999,True),(1000000000,False),(1000000001,False)):
            link,ns,_=load();ticks=iter([0,0,elapsed]);ns['time'].monotonic_ns=lambda:next(ticks);link.EP_OUT=Out()
            if success:self.assertTrue(link.usbwrite(b'A'))
            else:self.rejected(link,ns,lambda:link.usbwrite(b'A'))
            self.note('per-call-boundary-'+str(elapsed))
        link,ns,_=load();link.BULK_TOTAL_TIMEOUT_MS=2;ticks=iter([0,1000000,1500000]);ns['time'].monotonic_ns=lambda:next(ticks)
        link.EP_OUT=Out();self.assertTrue(link.usbwrite(b'A'));self.assertEqual(link.EP_OUT.calls[0][1],1);self.note('remaining-timeout-positive')
        link,ns,_=load();link.EP_OUT=Out();ticks=iter([0,119999000001]);ns['time'].monotonic_ns=lambda:next(ticks)
        self.rejected(link,ns,lambda:link.usbwrite(b'A'));self.assertEqual(link.EP_OUT.calls,[]);self.note('sub-ms-no-endpoint')
        for mode in (0,100,5000):
            link,ns,_=load();link.EP_IN=In([b'A',b'B'])
            self.assertEqual(link.usbread(2,maxtimeout=mode),b'AB')
            self.assertEqual([x[1] for x in link.EP_IN.calls],[1000,1000]);self.note('legacy-mode-'+str(mode))
        for location in ('clock','endpoint','logging'):
            link,ns,_=load();link.EP_OUT=Out();link.EP_IN=In([])
            def nested(*args):
                try:link.usbread(1)
                except ns['UsbTransferError']:pass
                return 0
            if location=='clock':ns['time'].monotonic_ns=nested
            elif location=='endpoint':link.EP_OUT=Out([lambda data,timeout:nested() or len(data)])
            else:link.verify_data=nested
            self.rejected(link,ns,lambda:link.usbwrite(b'A'))
            self.assertEqual(len(link.EP_OUT.calls),0 if location=='clock' else 1)
            self.assertEqual(link.EP_IN.calls,[]);self.note('swallowed-reentrancy-'+location)

    def test_real_swallowing_protocol_and_port(self):
        for method in ('ack','xread','send_param'):
            link,ns,_=load();link.EP_OUT=Out([OSError('failed')]);link.EP_IN=In([OSError('failed')]);p=protocol(link)
            if method=='ack':self.assertEqual(p.ack(),-1)
            elif method=='xread':self.assertEqual(p.xread(),-1)
            else:self.rejected(link,ns,lambda:p.send_param(b'parameter'))
            before=(len(link.EP_OUT.calls),len(link.EP_IN.calls))
            self.assertEqual(p.ack(),-1)
            self.rejected(link,ns,lambda:p.send_param(b'next'))
            self.assertEqual((len(link.EP_OUT.calls),len(link.EP_IN.calls)),before);self.note('actual-XFlash-'+method)
        link,ns,_=load();link.EP_OUT=Out();link.EP_IN=In([b'AB']);p=port(link)
        self.assertTrue(p.echo(b'AB'));self.note('actual-Port-echo')

    def test_close_reconnect_handshake_guards(self):
        link,ns,events=load();link.EP_OUT=Out([OSError('unknown prefix')]);link.EP_IN=In([])
        self.rejected(link,ns,lambda:link.usbwrite(b'AB'))
        p=port(link)
        for name in ('handshake','serial_handshake','run_handshake','run_serial_handshake'):
            self.rejected(link,ns,getattr(p,name));self.note('poison-'+name)
        self.rejected(link,ns,lambda:link.usbxmlread());self.note('poison-legacy-xml')
        link.close(reset=True);self.assertEqual(events,['dispose']);self.assertFalse(link.connected)
        self.rejected(link,ns,lambda:link.connect());self.assertEqual(events,['dispose']);self.note('poison-close-no-reset-reconnect-no-discovery')
        link,ns,events=load();link.EP_OUT=Out();self.assertTrue(link.usbwrite(b'A'))
        link.close(reset=True);self.assertEqual(events,['reset','attach','dispose',('sleep',2)])
        self.assertFalse(link.connect());self.assertEqual(events[-1],'discover');self.assertIsNone(link._io_failure);self.note('healthy-reconnect-not-poisoned')
        link,ns,events=load();link._io_failure='prior failure'
        ns['usb'].util.dispose_resources=lambda dev:(_ for _ in ()).throw(OSError('dispose failed'))
        with self.assertRaises(OSError):link.close(reset=True)
        self.assertFalse(link.connected);self.assertIsNone(link.device);self.rejected(link,ns,lambda:link.connect());self.note('dispose-error-retains-latch')

    def test_final_read_and_optional_write_swallowing(self):
        for tree in ('original','candidate'):
            link,ns,_=load();link.EP_OUT=Out()
            link.EP_IN=In([pack('<III',0xFEEEEEEF,1,8),b'PAYLOAD!',OSError('final read status failed')])
            p=protocol(link,tree)
            if tree=='original':self.assertEqual(p.readflash(0,8,'',display=False),b'PAYLOAD!')
            else:self.rejected(link,ns,lambda:p.readflash(0,8,'',display=False))
            self.assertIsNotNone(link._io_failure);self.assertEqual(len(link.EP_IN.calls),3)
            self.note(tree+'-readflash-full-data-final-failure')
            link,ns,_=load();link.EP_OUT=Out()
            # send_param, final write status, DEVICE_CTRL status, optional-command
            # status all succeed; actual xread then swallows its header exception.
            zero=[pack('<III',0xFEEEEEEF,1,4),pack('<I',0)]
            link.EP_IN=In(zero*4+[OSError('optional response failed')]);p=protocol(link,tree)
            if tree=='original':self.assertTrue(p.writeflash(0,512,wdata=b'X'*512,display=False))
            else:self.rejected(link,ns,lambda:p.writeflash(0,512,wdata=b'X'*512,display=False))
            self.assertIsNotNone(link._io_failure);before=len(link.EP_OUT.calls)
            self.rejected(link,ns,lambda:link.usbwrite(b'next'));self.assertEqual(len(link.EP_OUT.calls),before)
            self.note(tree+'-writeflash-optional-response-failure')

    def test_actual_pyusb_semantics(self):
        path=FIXTURES/'pyusb-replay.py'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'d75e32bf2d5984bb3d216fcb77c008afa6798dddf9debdb169831babf3ed3c69')
        spec=importlib.util.spec_from_file_location('source_pyusb_replay',path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        link,ns,_=load();model=mod.Model([(-7,2,None),(0,2,None)]);link.EP_OUT=model.out
        self.assertTrue(link.usbwrite(b'ABCD'));self.assertEqual(bytes(model.acknowledged),b'ABCD');self.note('actual-PyUSB-positive-timeout-suffix')
        for fast in (False,True):
            link,ns,_=load();model=mod.Model([(-7,2,b'AB'),(0,2,b'CD')]);link.EP_IN=model.into;link.fast=fast
            self.assertEqual(link.usbread(4,w_max_packet_size=4),b'ABCD');self.note('actual-PyUSB-read-'+str(fast))
        link,ns,_=load();model=mod.Model([(-1,2,None)]);link.EP_OUT=model.out
        self.rejected(link,ns,lambda:link.usbwrite(b'ABCD'));self.assertEqual(len(model.calls),1)
        self.rejected(link,ns,lambda:link.usbwrite(b'ABCD'));self.assertEqual(len(model.calls),1);self.note('actual-PyUSB-error-unknown-no-retry')

    def test_original_negative_controls(self):
        link,ns,_=load('original');link.EP_OUT=Out([0]*32+[RuntimeError('fixture stop')]*3)
        self.assertFalse(link.usbwrite(b'A'));self.assertEqual(len(link.EP_OUT.calls),35);self.note('original-zero-progress-32-iterations')
        link,ns,_=load('original');link.EP_OUT=Out([OSError('unknown partial'),1])
        self.assertTrue(link.usbwrite(b'A'));self.assertEqual([x[0] for x in link.EP_OUT.calls],[b'A',b'A']);self.note('original-exception-resends-prefix')
        link,ns,_=load('original');link.EP_OUT=Out([3]);self.assertTrue(link.usbwrite(b'A'));self.note('original-overlong-success')
        link,ns,_=load('original');link.EP_OUT=Out([ns['usb'].core.USBError('pipe error')]);self.assertTrue(link.usbwrite(b''));self.note('original-ZLP-error-success')
        link,ns,_=load();p=protocol(link);p.usbwrite=lambda data:False;p.status=lambda:0
        self.assertTrue(p.send_param(b'parameter'));self.note('original-send_param-False-swallowed')


if __name__ == '__main__':
    ORIGINAL, CANDIDATE, FIXTURES, OUTPUT = configure_paths()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    record={'status':'PASS' if result.wasSuccessful() else 'FAIL','methods':result.testsRun,
            'controls':len(CONTROLS),'names':CONTROLS,
            'source_sha256':hashlib.sha256((CANDIDATE/REL).read_bytes()).hexdigest(),
            'scope':'Actual selected source methods; scripted endpoints/PyUSB AST callbacks only. No USB imports or discovery.'}
    (OUTPUT/'test-result.json').write_text(json.dumps(record,indent=2)+'\n')
    raise SystemExit(not result.wasSuccessful())
