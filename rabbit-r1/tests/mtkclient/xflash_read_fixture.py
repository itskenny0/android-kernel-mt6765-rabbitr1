"""Scripted endpoint fixture for actual XFlash/USB AST methods.
Only Python standard-library imports; source modules are never imported.
"""
import array
import ast
import builtins
import copy
import logging
import struct
from pathlib import Path
from queue import Queue
from threading import Thread
from types import SimpleNamespace as NS

SOURCE = None
XPATH = 'mtkclient/Library/DA/xflash/xflash_lib.py'
USBPATH = 'mtkclient/Library/Connection/usblib.py'
MAGIC = 0xFEEEEEEF


def configure_source(source):
    global SOURCE
    SOURCE = source


def parse(relative):
    return ast.parse((SOURCE / relative).read_text(), filename=str(SOURCE / relative))

def method(relative, cls, name, env):
    parent = next(n for n in parse(relative).body if isinstance(n, ast.ClassDef) and n.name == cls)
    node = copy.deepcopy(next(n for n in parent.body if isinstance(n, ast.FunctionDef) and n.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE / relative), 'exec'), env)
    return env[name]

def header(length, magic=MAGIC, datatype=1):
    return struct.pack('<III', magic, datatype, length)

def terminal(value=0, magic=MAGIC, length=4):
    return [header(length, magic), struct.pack('<I', value) + b'\x00' * max(0, length - 4)]

def create(chunks, writer_failure=None):
    messages, threads, events = [], [], []
    env = dict(pack=struct.pack, unpack=struct.unpack, array=array, logging=logging,
               Queue=Queue, DeviceClass=object, time=NS(monotonic_ns=lambda: 0),
               usb=NS(), max_xml_data_length=4096)
    definitions = [copy.deepcopy(n) for n in parse(USBPATH).body
                   if isinstance(n, ast.ClassDef) and n.name in ('UsbTransferError', 'UsbClass')]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE / USBPATH), 'exec'), env)
    cdc = object.__new__(env['UsbClass'])
    cdc._io_failure = None; cdc._io_active = False; cdc._io_last_time = None
    cdc.timeout = 1000; cdc.maxsize = 512; cdc.fast = False; cdc.queue = Queue()
    cdc.loglevel = logging.INFO; cdc.connected = False
    cdc.verify_data = lambda *args: None
    for name in ('info', 'debug', 'error', 'warning'):
        setattr(cdc, name, messages.append)
    class In:
        wMaxPacketSize = 512
        def __init__(self): self.steps = list(chunks)
        def read(self, length, timeout):
            events.append(['read', length, timeout])
            if not self.steps:
                raise OSError('scripted input exhausted')
            value = self.steps.pop(0)
            if isinstance(value, BaseException): raise value
            return value
    class Out:
        wMaxPacketSize = 512
        def write(self, data, timeout):
            events.append(['write', bytes(data).hex(), timeout]); return len(data)
    cdc.EP_IN = In(); cdc.EP_OUT = Out()
    writer_env = dict(Queue=Queue)
    writer_node = next(n for n in parse('mtkclient/Library/thread_handling.py').body
                       if isinstance(n, ast.FunctionDef) and n.name == 'writedata')
    class FileBoundary:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def write(self, data):
            if writer_failure == 'short': return self.stream.write(data[:len(data)//2])
            if writer_failure == 'raise': raise OSError('injected local file write failure')
            return self.stream.write(data)
        def __exit__(self, *args):
            self.stream.close()
            if writer_failure == 'close': raise OSError('injected local file close failure')
    if writer_failure:
        writer_env['open'] = lambda *a, **kw: FileBoundary(builtins.open(*a, **kw))
    exec(compile(ast.Module(body=[writer_node], type_ignores=[]),
                 str(SOURCE / 'mtkclient/Library/thread_handling.py'), 'exec'), writer_env)
    def tracked_thread(*args, **kwargs):
        thread = Thread(*args, **kwargs); threads.append(thread); return thread
    env.update(Queue=Queue, Thread=tracked_thread, writedata=writer_env['writedata'],
               progress=lambda **kw:NS(update=lambda n:None, done=lambda:None),
               DataType=NS(DT_PROTOCOL_FLOW=1),
               DaStorage=NS(MTK_DA_STORAGE_EMMC=1),
               EmmcPartitionType=NS(MTK_DA_EMMC_PART_USER=8),
               NandExtension=lambda:NS(**{k:0 for k in ['cellusage','addr_type','bin_type','region',
                   'format_level','sys_slc_percent','usr_slc_percent','phy_max_size']}))
    cls = type('ActualRead', (), {name:method(XPATH, 'DAXFlash', name, env) for name in
                                 ['readflash','ack','xsend','status','send_param','cmd_read_data']})
    x = cls()
    x.usbread = cdc.usbread; x.usbwrite = cdc.usbwrite
    x.error = x.warning = messages.append
    x.mtk = NS(config=NS(chipconfig=NS(dacode=0x6765),guiprogress=None),port=NS(cdc=cdc,close=cdc.close))
    x.daconfig = NS(storage=NS(get_storage=lambda part,length:(1,8,length)))
    x.cmd = NS(MAGIC=MAGIC,READ_DATA=0x10005)
    x.data_type = NS(DT_PROTOCOL_FLOW=1)
    x.eh = NS(status=lambda status:hex(status))
    x.get_packet_length = lambda:NS(read_packet_length=1048576)
    x.cmd_read_data = lambda **kw:True  # Handshake tested separately below.
    return x, cdc, messages, threads, events, env
