#!/usr/bin/env python3
"""Actual GPT export/parsing with local synthetic reads; no USB imports or device I/O."""
import argparse
import ast
from enum import Enum
import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
from struct import pack, pack_into, unpack_from
import struct
from types import SimpleNamespace as NS
import zlib


def compile_classes(path, ns, wanted=None):
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and (wanted is None or n.name in wanted)]
    for n in nodes:n.keywords=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)


def load(source):
    lib=source/'mtkclient/Library'
    ns=dict(logging=logging,BytesIO=BytesIO,Enum=Enum,pack=pack,DaHandler=lambda *a,**k:None)
    compile_classes(lib/'gui_utils.py',ns,{'structhelper_io'})
    compile_classes(lib/'Partitions/__init__.py',ns,{'generic','partf'})
    ns['generic']._generic__logger=logging.getLogger('gpt-fixture')
    ns['generic'].error=lambda *args:None
    compile_classes(lib/'Partitions/gpt.py',ns,{'gpt'})
    compile_classes(lib/'Partitions/mbr.py',ns)
    compile_classes(lib/'realtime.py',ns,{'RealTimeHandler','MTKFileHandler'})
    compile_classes(lib/'partition.py',ns,{'Partition'})
    return ns


def fixture(count=128,stride=128,start=2,first=None):
    end=start*512+count*stride
    if first is None:first=(end+511)//512
    disk=bytearray(256*512)
    disk[510:512]=b'\x55\xaa';disk[450]=0xee
    pack_into('<II',disk,454,1,255)
    table=bytearray(count*stride)
    if count:
        table[:16]=bytes(range(1,17));table[16:32]=bytes(range(17,33))
        pack_into('<QQQ',table,32,max(first,34),max(first,34)+7,0)
        table[56:66]='alpha'.encode('utf-16-le')
    disk[start*512:end]=table
    pack_into('<8sIIIIQQQQ16sQIII',disk,512,b'EFI PART',0x10000,92,0,0,1,255,first,254,
              bytes(range(33,49)),start,count,stride,zlib.crc32(table))
    pack_into('<I',disk,528,zlib.crc32(disk[512:604]))
    return bytes(disk),((end+511)//512)*512


def export(ns,data,fault=None,size=None):
    reads=[]
    def readflash(addr,length,**kw):
        reads.append((addr,length))
        value=data[addr:addr+length]
        if fault and addr==0 and length==fault[0]:
            if isinstance(fault[1],Exception):raise fault[1]
            return value[:fault[1]]
        return value
    if size is None:size=(unpack_from('<Q',data,544)[0]+1)*512
    owner=NS(config=NS(pagesize=512),daloader=NS(daconfig=NS(storage=NS(flashsize=size)),readflash=readflash))
    obj=object.__new__(ns['Partition']);obj.mtk=owner;obj.config=owner.config;obj.read_pmt=lambda:None
    settings=NS(gpt_num_part_entries=0,gpt_part_entry_size=0,gpt_part_entry_start_lba=0)
    return obj.get_gpt(settings),reads


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--original',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--captured-head',type=Path)
    ap.add_argument('--failed-export',type=Path)
    a=ap.parse_args();root=Path('/rabbitr1')
    for p in (a.source,a.original,a.out,a.captured_head,a.failed_export):
        if p is not None and not p.resolve().is_relative_to(root):ap.error('Paths must stay under /rabbitr1')
    a.out.mkdir(parents=True,exist_ok=False)
    new=load(a.source);old=load(a.original);controls=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        controls.append(name)
    helper=Path(__file__).with_name('prepare-flash.py')
    node=next(n for n in ast.parse(helper.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='parse_gpt')
    ns={'struct':struct,'zlib':zlib}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(helper),'exec'),ns)
    strict=ns['parse_gpt']
    def rejected(name,fn):
        try:fn()
        except (ValueError,OSError):controls.append(name)
        else:raise AssertionError(name)
    data,length=fixture()
    (before,g),reads=export(old,data)
    check('actual original export is exactly33sectors',len(before)==16896 and before==data[:16896])
    rejected('unchanged strict consumer rejects original truncation',lambda:strict(before))
    for label,kwargs in [('standard128x128',{}),('arrayLBA4',{'start':4}),('129entries-rounded',{'count':129}),('256-byte-entries',{'stride':256}),('one-entry-array',{'count':1})]:
        data,length=fixture(**kwargs);(got,g),reads=export(new,data)
        check(label+' exact declared rounded bytes',got==data[:length] and len(got)==length)
        check(label+' actual GPT entry parsed',len(g.partentries)==1 and g.partentries[0].name=='alpha')
        check(label+' unchanged CRC consumer accepts',strict(got)==strict(data))
        check(label+' final read exact declared extent',reads[-1]==(0,length))
    data,length=fixture()
    for label,fault in [('short-one-byte',(length,length-1)),('short-one-sector',(length,length-512)),('empty',(length,0)),('read-EIO',(length,OSError('fixture EIO')))]:
        rejected(label,lambda fault=fault:export(new,data,fault=fault))
    for label,at,fmt,val in [('array overlaps header',584,'<Q',1),('array overlaps usable area',552,'<Q',33),('zero count',592,'<I',0),('invalid stride',596,'<I',129),('wrong currentLBA',536,'<Q',2)]:
        bad=bytearray(data);pack_into(fmt,bad,at,val)
        rejected(label,lambda bad=bad:export(new,bytes(bad)))
    rejected('declared export exceeds device',lambda:export(new,data,size=length-512))
    (good,_),_=export(new,data)
    bad=bytearray(good);bad[528]^=1
    rejected('header CRC remains mandatory',lambda:strict(bytes(bad)))
    bad=bytearray(good);bad[17000]^=1
    rejected('array CRC remains mandatory including omitted last sector',lambda:strict(bytes(bad)))
    captured=None
    if a.captured_head is not None:
        data=a.captured_head.read_bytes();(before,_),_=export(old,data);(got,g),reads=export(new,data)
        check('private actual capture corrected17408bytes',len(got)==17408 and got==data[:17408])
        check('private actual47partitions unchanged',len(g.partentries)==47 and strict(got)==strict(data))
        if a.failed_export is not None:check('actual failed export exactly reproduced',before==a.failed_export.read_bytes())
        (a.out/'corrected-gpt.bin').write_bytes(got)
        captured={'head_sha256':hashlib.sha256(data).hexdigest(),'corrected_sha256':hashlib.sha256(got).hexdigest(),'bytes':len(got)}
    pins={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [a.source/'mtkclient/Library/partition.py',a.original/'mtkclient/Library/partition.py',helper,Path(__file__)]}
    r={'status':'pass','count':len(controls),'controls':controls,'actual_source_classes':['Partition','MTKFileHandler','RealTimeHandler','generic','gpt','mbr','structhelper_io'],'mock_boundary':'In-memory daloader.readflash only; constructors/logging boundary; no USB or target imports','private_capture':captured,'pins':pins}
    (a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
    print('PASS',len(controls),'GPT export controls')


if __name__=='__main__':main()
