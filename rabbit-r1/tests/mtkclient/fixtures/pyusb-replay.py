#!/usr/bin/env python3
"""Original PyUSB AST bodies with synthetic Python callbacks in place of libusb.
No usb import, shared-library loading, discovery, original constructor or device I/O.
"""
import array,ast,ctypes,errno,hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;SRC=HERE/'pyusb'
PINS={'core.py':'aa6089accb592aecb18e1eb8bba9ea62b0a1eee7df4e564b8d4b5a0877bc7c7c','libusb1.py':'ea13c99dbe9667904ad1d3f364a147cf75bfdf98111b3426ceec20d055f3203d','_interop.py':'4a216f41bebf83dfa6fa05169db5589f29c343c2742eb7137ea4f431968870c2','util.py':'4be8a03d725fb82f773ed79b890ba3167781e9cee722015228d369c25cd19294'}
sha=lambda b:hashlib.sha256(b).hexdigest()
DATA={k:(SRC/k).read_bytes() for k in PINS}
assert {k:sha(v) for k,v in DATA.items()}==PINS
ns={'array':array,'errno':errno,'c_int':ctypes.c_int,'c_ubyte':ctypes.c_ubyte,'cast':ctypes.cast,'POINTER':ctypes.POINTER,'byref':ctypes.byref,'_lib':NS()}
provenance=[]
def compile_nodes(name,nodes):
 for node in nodes:
  provenance.append({'file':name,'kind':type(node).__name__,'name':getattr(node,'name',None),'line':node.lineno,'end_line':node.end_lineno,'source_sha256':sha(ast.get_source_segment(DATA[name].decode(),node).encode())})
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SRC/name),'exec'),ns)
def select_functions(name,wanted):
 compile_nodes(name,[n for n in ast.parse(DATA[name]).body if isinstance(n,ast.FunctionDef) and n.name in wanted])
def select_classes(name,members):
 for node in ast.parse(DATA[name]).body:
  if isinstance(node,ast.ClassDef) and node.name in members:
   if members[node.name] is not None:
    node.body=[n for n in node.body if isinstance(n,ast.FunctionDef) and n.name in members[node.name]]
    node.bases=[];node.keywords=[]
   compile_nodes(name,[node])
# Exact declarations/primitive conversion functions needed by selected bodies only.
compile_nodes('util.py',[n for n in ast.parse(DATA['util.py']).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and (t.id.startswith('ENDPOINT_TYPE_') or t.id=='_ENDPOINT_TRANSFER_TYPE_MASK') for t in n.targets)])
select_functions('util.py',{'endpoint_type','create_buffer'});ns['util']=NS(**{k:ns[k] for k in ('endpoint_type','create_buffer','ENDPOINT_TYPE_BULK','ENDPOINT_TYPE_INTR','ENDPOINT_TYPE_ISO')})
select_functions('_interop.py',{'as_array'});ns['_interop']=NS(as_array=ns['as_array'])
select_classes('core.py',{'USBError':None,'USBTimeoutError':None,'Endpoint':{'read','write'},'Device':{'read','write','__get_timeout'}})
compile_nodes('libusb1.py',[n for n in ast.parse(DATA['libusb1.py']).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and (t.id.startswith('LIBUSB_ERROR_') or t.id=='LIBUSB_SUCCESS' or t.id in ('_libusb_errno','_str_error_map')) for t in n.targets)])
select_functions('libusb1.py',{'_check','_strerror'});select_classes('libusb1.py',{'_LibUSB':{'__write','__read'}})

class Model:
 """Endpoint adapter composed only of selected original methods and fake context."""
 def __init__(self,script):
  self.script=list(script);self.calls=[];self.acknowledged=bytearray();self.physically_touched=bytearray()
  helper=ns['_LibUSB']()
  def callback(handle,ep,pointer,length,transferred,timeout):
   assert self.script,'unexpected synthetic libusb call'
   result,count,payload=self.script.pop(0)
   direction='read' if ep&0x80 else 'write'
   self.calls.append({'direction':direction,'length':length,'timeout_ms':timeout,'result':result,'transferred':count,'request':bytes(pointer[:length]).hex() if direction=='write' else None})
   if direction=='read':
    assert len(payload)>=max(count,0)
    for i in range(min(max(count,0),length)):pointer[i]=payload[i]
   else:
    self.physically_touched.extend(bytes(pointer[:min(max(count,0),length)]))
    if result==0 or result==ns['LIBUSB_ERROR_TIMEOUT'] and count>0:self.acknowledged.extend(bytes(pointer[:min(max(count,0),length)]))
   transferred._obj.value=count
   return result
  def write(handle,ep,intf,data,timeout):return helper._LibUSB__write(callback,handle,ep,intf,data,timeout)
  def read(handle,ep,intf,data,timeout):return helper._LibUSB__read(callback,handle,ep,intf,data,timeout)
  backend=NS(bulk_write=write,bulk_read=read,intr_write=write,intr_read=read,iso_write=write,iso_read=read)
  device=ns['Device']();device._Device__default_timeout=1000
  device._ctx=NS(backend=backend,handle=NS(handle=None),setup_request=lambda d,e:(NS(bInterfaceNumber=0),NS(bEndpointAddress=e.bEndpointAddress,bmAttributes=ns['ENDPOINT_TYPE_BULK'])))
  self.out=ns['Endpoint']();self.out.device=device;self.out.bEndpointAddress=1
  self.into=ns['Endpoint']();self.into.device=device;self.into.bEndpointAddress=0x81


def run():
 cases=[]
 for name,status,count in [('full success',0,8),('short success',0,3),('partial timeout',-7,3),('complete timeout',-7,8),('zero timeout',-7,0),('partial pipe failure',-9,3),('partial disconnect',-4,3)]:
  for direction in ('write','read-size','read-buffer'):
   m=Model([(status,count,b'abcdefgh')]);exception=None;result=None
   try:
    if direction=='write':result=m.out.write(b'ABCDEFGH',timeout=37)
    elif direction=='read-size':result=m.into.read(8,timeout=37)
    else:result=m.into.read(array.array('B',bytes(8)),timeout=37)
   except ns['USBError'] as exc:exception={'type':type(exc).__name__,'code':exc.backend_error_code,'attributes':sorted(exc.__dict__)}
   expected_error=status not in (0,-7) or status==-7 and count==0
   assert (exception is not None)==expected_error,(name,direction,result,exception)
   if not expected_error:
    assert type(result) is (array.array if direction=='read-size' else int)
    assert (result.tobytes()==b'abcdefgh'[:count]) if direction=='read-size' else result==count
   else:assert 'transferred' not in exception['attributes']
   assert len(m.calls)==1 and m.calls[0]['timeout_ms']==37
   cases.append({'case':name,'api':direction,'return_type':None if exception else type(result).__name__,'return':None if exception else (result.tobytes().hex() if isinstance(result,array.array) else result),'exception':exception,'libusb_callback':m.calls[0],'physically_touched':m.physically_touched.hex(),'acknowledged':m.acknowledged.hex()})
 # Demonstrate correct suffix continuation using the real partial-timeout branch.
 m=Model([(-7,3,b''),(0,5,b'')]);payload=b'ABCDEFGH'
 n=m.out.write(payload,timeout=10);assert type(n) is int and n==3
 assert m.out.write(payload[n:],timeout=8)==5
 assert [c['request'] for c in m.calls]==[payload.hex(),payload[3:].hex()]
 assert bytes(m.acknowledged)==payload
 cases.append({'case':'suffix-only continuation after positive partial-timeout count','calls':m.calls,'acknowledged':m.acknowledged.hex()})
 assert {k:sha((SRC/k).read_bytes()) for k in PINS}==PINS
 result={'scope':'selected original PyUSB source, synthetic libusb callbacks only','release':'v1.3.1','commit':'89ea84d6c9e4cb81bc6e40d7df849d0cbd7dfd47','source_pins':PINS,'passed':True,'cases':len(cases),'checks':cases,'selected_source':provenance,'script_sha256':sha(Path(__file__).read_bytes()),'limitations':['No usb import, libusb library, real context/discovery/endpoint or original constructor','The injected callback models libusb status and transferred outputs; it does not prove a hardware response','Timeout with positive count loses timeout reason at this PyUSB boundary','Other errors raise without transferred count despite modeled partial side effects']}
 (HERE/'pyusb-result.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({'passed':True,'cases':len(cases),'source_files':len(PINS),'result_sha256':sha((HERE/'pyusb-result.json').read_bytes())}))
if __name__=='__main__':run()
