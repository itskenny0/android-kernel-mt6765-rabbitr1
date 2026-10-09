#!/usr/bin/env python3
"""Replay XFlash read acceptance against an explicit prepared source tree.
No target modules, USB package, discovery or DA/device operation is imported/run.
Query/storage/initial command are modeled except a separate actual cmd_read_data case.
"""
import argparse,ast,copy,hashlib,importlib.util,json,os,shlex,stat,struct,subprocess,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
SOURCE = OUTPUT = None
XPATH = 'mtkclient/Library/DA/xflash/xflash_lib.py'
USBPATH = 'mtkclient/Library/Connection/usblib.py'
MAGIC = 0xFEEEEEEF
SOURCE_FILES = (
    XPATH, 'mtkclient/Library/DA/xflash/xflash_param.py',
    'mtkclient/Library/thread_handling.py', USBPATH,
    'mtkclient/Library/DA/mtk_daloader.py', 'mtkclient/Library/DA/mtk_da_handler.py',
    'mtkclient/Library/mtk_main.py', 'mtk.py',
)


def configure_paths():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path,
                        help='Prepared mtkclient source tree beneath /rabbitr1')
    parser.add_argument('--out', required=True, type=Path,
                        help='New results directory beneath /rabbitr1')
    parser.add_argument('--cli-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    workspace = Path('/rabbitr1')

    def confined_path(path, label, required):
        # Reject a lexical escape before consulting the filesystem. Reject every
        # symlink component before following it, including source file parents.
        if (not path.is_absolute() or not path.is_relative_to(workspace) or
                path == workspace or '..' in path.parts):
            parser.error(label + ' must be an absolute path below /rabbitr1 without ..')
        current = workspace
        for component in path.relative_to(workspace).parts:
            current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                if required:
                    parser.error('Missing ' + label + ': ' + str(current))
                return path
            except OSError as error:
                parser.error(label + ': ' + str(error))
            if stat.S_ISLNK(info.st_mode):
                parser.error(label + ' must not contain symlinks')
            if current != path and not stat.S_ISDIR(info.st_mode):
                parser.error(label + ' parents must be directories')
        return path

    source = confined_path(args.source, '--source', True)
    output = confined_path(args.out, '--out', False)
    if not source.is_dir():
        parser.error('--source must be a directory')
    if output.exists():
        parser.error('--out must be a new directory')
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error('--out and --source must not contain each other')
    for relative in SOURCE_FILES:
        path = confined_path(source / relative, 'source input', True)
        if not path.is_file():
            parser.error('Source input is not a regular file: ' + relative)
    output.mkdir(parents=True)
    return source, output, args.cli_child


def source_pins():
    return [{'path': relative, 'bytes': (SOURCE / relative).stat().st_size,
             'sha256': hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest()}
            for relative in SOURCE_FILES]


# Load only this standard-library fixture, not a source-tree Python module.
sys.dont_write_bytecode = True
helper_path = Path(__file__).resolve().parent.parent / 'tests/mtkclient/xflash_read_fixture.py'
helper_spec = importlib.util.spec_from_file_location('xflash_read_fixture', helper_path)
fixture = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(fixture)
parse, method, header, terminal = fixture.parse, fixture.method, fixture.header, fixture.terminal
original_create = fixture.create

def create(chunks,fault=None):
 x,cdc,messages,threads,events,env=original_create(chunks)
 writes=[]; originals=[]; query_calls=[]; command_calls=[]; publication_calls=[]; streams=[]
 os_boundary=NS(**{n:getattr(os,n) for n in ('fspath','path','sep','lstat','fchmod','fdopen','close','unlink','fstat','replace')})
 class FileBoundary:
  def __init__(self,stream):self.stream=stream;self.close_attempts=0;streams.append(self)
  def write(self,data):
   writes.append(len(data))
   if fault=='write-raise':raise OSError('injected local write failure')
   if fault=='write-bool':self.stream.write(data);return True
   if fault=='write-none':return None
   if fault=='write-short':return self.stream.write(data[:len(data)//2])
   if fault=='write-lying':self.stream.write(data[:len(data)//2]);return len(data)
   return self.stream.write(data)
  def fileno(self):return self.stream.fileno()
  def flush(self):
   if fault=='flush':raise OSError('injected local flush failure')
   return self.stream.flush()
  def close(self):
   self.close_attempts+=1
   if fault=='close-before' and self.close_attempts==1:raise OSError('injected close before release failure')
   was_closed=self.stream.closed
   self.stream.close()
   if fault=='close' and not was_closed:raise OSError('injected local close failure')
 def fdopen(*a,**kw):
  if fault=='fdopen':raise OSError('injected local fdopen failure')
  return FileBoundary(os.fdopen(*a,**kw))
 os_boundary.fdopen=fdopen
 def publish(src,dst):
  publication_calls.append([str(src),str(dst)])
  if fault=='replace':raise OSError('injected local replace failure')
  return os.replace(src,dst)
 os_boundary.replace=publish
 temp_boundary=NS(mkstemp=tempfile.mkstemp)
 if fault=='temp-open':temp_boundary.mkstemp=lambda **kw:(_ for _ in ()).throw(OSError('injected temporary open failure'))
 if fault=='fchmod':os_boundary.fchmod=lambda *a:(_ for _ in ()).throw(OSError('injected permission failure'))
 def done():
  if fault=='progress-done':raise OSError('injected progress completion failure')
 env.update(os=os_boundary,stat=stat,tempfile=temp_boundary,progress=lambda **kw:NS(update=lambda n:None,done=done))
 setattr(type(x),'_readflash_destination',staticmethod(method(XPATH,'DAXFlash','_readflash_destination',env)))
 def query():
  query_calls.append(True)
  if fault=='query-none':return None
  if fault=='query-zero':return NS(read_packet_length=0)
  if fault=='query-negative':return NS(read_packet_length=-1)
  if fault=='query-bool':return NS(read_packet_length=True)
  if fault=='query-poison':
   try:cdc._io_fail('injected swallowed query transport failure')
   except env['UsbTransferError']:pass
  return NS(read_packet_length=1048576)
 def command(**kw):
  command_calls.append(kw)
  return False if fault=='command-false' else True
 x.get_packet_length=query;x.cmd_read_data=command
 if fault in ('ack-minus-one','ack-false'):x.ack=lambda **kw:-1 if fault=='ack-minus-one' else False
 if fault=='ack-usb':
  cdc.EP_OUT.write=lambda *a,**kw:(_ for _ in ()).throw(OSError('injected ACK endpoint failure'))
 original_read=x.usbread
 reads=[]
 def read(*a,**kw):
  reads.append([a[0],kw])
  if fault=='direct-short-header' and len(reads)==1:return b'SHORT'
  if fault=='direct-short-data' and len(reads)==2:return b'AB'
  if fault=='direct-overlong-data' and len(reads)==2:return b'ABCDEFGHI'
  if fault=='direct-short-terminal' and len(reads)==3:return b'SHORT'
  if fault=='direct-short-status' and len(reads)==4:return b'AB'
  try:return original_read(*a,**kw)
  except BaseException as error:originals.append(error);raise
 x.usbread=read
 return x,cdc,messages,threads,events,env,NS(writes=writes,originals=originals,queries=query_calls,commands=command_calls,publications=publication_calls,reads=reads,streams=streams)


def run_case(directory,name,chunks=None,*,length=8,file=False,fault=None,success=False,poison=True,
             data=b'ABCDEFGH',selected=None,packet=None,existing=True,path_kind=None,initial_poison=False):
 chunks=[header(8),b'ABCDEFGH',*terminal()] if chunks is None else chunks
 x,cdc,messages,threads,events,env,trace=create(chunks,fault)
 if selected is not None:x.daconfig.storage.get_storage=lambda part,requested:selected
 if packet is not None:x.get_packet_length=lambda:NS(read_packet_length=packet)
 destination=directory/(name+'.bin')
 old=b'PREVIOUS DUMP BYTES'
 def fingerprint(path):
  try:info=path.lstat()
  except FileNotFoundError:return None
  content=path.read_bytes().hex() if stat.S_ISREG(info.st_mode) else os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
  return [info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_uid,info.st_gid,content]
 if file and existing:destination.write_bytes(old);os.chmod(destination,0o644)
 if path_kind=='missing-parent':destination=directory/'missing'/name
 elif path_kind=='symlink':destination.unlink(missing_ok=True);destination.symlink_to(directory/'some-target')
 elif path_kind=='hardlink':os.link(destination,directory/(name+'.link'))
 elif path_kind=='directory':destination.unlink(missing_ok=True);destination.mkdir()
 elif path_kind=='fifo':destination.unlink(missing_ok=True);os.mkfifo(destination)
 elif path_kind=='parent-symlink':
  link=directory/(name+'-parent');link.symlink_to(directory,target_is_directory=True);destination=link/(name+'-target')
 elif path_kind=='dotdot':destination=directory/'..'/directory.name/(name+'-target')
 before_destination=fingerprint(destination) if file else None
 if initial_poison:cdc._io_failure='existing poison'
 result=None;error=None;caught=None
 try:result=x.readflash(0,length,str(destination) if file else '',parttype='user',display=True)
 except BaseException as exc:caught=exc;error={'type':type(exc).__name__,'message':str(exc)}
 assert not threads, 'candidate unexpectedly started a worker'
 assert all(s.stream.closed for s in trace.streams),name+' leaked open stream'
 assert not list(directory.glob('.*.tmp')),name+' leaked temporary file'
 after=destination.read_bytes() if file and destination.exists() and destination.is_file() else None
 if success:
  assert error is None,(name,error)
  if file:
   assert result is True and after==data,(name,result,after)
   assert stat.S_IMODE(destination.stat().st_mode)==0o600,name
   assert len(trace.publications)==1,name
  else:assert type(result) is bytearray and result==data,(name,result)
  assert cdc._io_failure is None,name
 else:
  assert error is not None,(name,result)
  assert bool(cdc._io_failure)==poison,(name,cdc._io_failure,error)
  assert result is None,name
  if file:assert fingerprint(destination)==before_destination,name+' changed unsuccessful destination'
  if not poison:cdc.check_io()
  if file and not path_kind:assert after==(old if existing else None),(name,after)
  if poison:
   count=len(events)
   try:cdc.usbread(1)
   except env['UsbTransferError']:pass
   else:raise AssertionError(name+' allowed bulk read after poison')
   assert len(events)==count,name
   try:cdc.usbwrite(b'x')
   except env['UsbTransferError']:pass
   else:raise AssertionError(name+' allowed bulk write after poison')
  if trace.originals:assert caught is trace.originals[-1],name+' replaced original USB exception'
 if path_kind or fault in ('temp-open','fdopen','fchmod') or selected is False or initial_poison:
  assert not trace.queries and not trace.commands and not events,name
 if fault and fault.startswith('query-'):
  assert len(trace.queries)==1 and not trace.commands and not events,name
 if fault=='command-false':assert not events,name
 row={'case':name,'success':success,'error':error,'poison':cdc._io_failure,
      'file_preserved':not success and file and after==old,'file_bytes':len(after) if after is not None else None,
      'returned_bytes':len(result) if isinstance(result,bytearray) else None,
      'remaining_scripted_reads':len(cdc.EP_IN.steps),'events':events,'application_reads':trace.reads,
      'write_sizes':trace.writes,'query_calls':len(trace.queries),'command_calls':trace.commands,
      'publication_calls':trace.publications,'temp_files_left':[]}
 return row


def cli_child():
 x,cdc,messages,threads,events,env,trace=create([header(8),b'ABCDEFGH',*terminal(1)])
 config=x.mtk.config;config.pagesize=512;config.generatekeys=False;mtk=x.mtk
 L=type('Loader',(),{'readflash':method('mtkclient/Library/DA/mtk_daloader.py','DAloader','readflash',env)})
 loader=L();loader.da=x;loader.config=config;loader.get_partition_data=lambda parttype:[NS(name='boot_a',sector=3,sectors=1)]
 mtk.daloader=loader
 H=type('Handler',(),{n:method('mtkclient/Library/DA/mtk_da_handler.py','DaHandler',n,env) for n in ('da_read','handle_da_cmds')})
 handler=H();handler.mtk=mtk;handler.config=config;handler.error=handler.info=messages.append
 handler.connect=lambda original,directory:original;handler.configure_da=lambda original:original
 env.update(DaHandler=lambda *args:handler,os=os,sys=sys,loglevel=20)
 parent=next(n for n in parse('mtkclient/Library/mtk_main.py').body if isinstance(n,ast.ClassDef) and n.name=='Main')
 run=next(n for n in parent.body if isinstance(n,ast.FunctionDef) and n.name=='run')
 branch=next(n for n in ast.walk(run) if isinstance(n,ast.If) and n.orelse and n.orelse[0].lineno==708)
 wrapper=ast.parse('def dispatch(self,mtk,cmd,directory):\n pass\n').body[0];wrapper.body=copy.deepcopy(branch.orelse);ast.fix_missing_locations(wrapper)
 exec(compile(ast.Module(body=[wrapper],type_ignores=[]),str(SOURCE/'mtkclient/Library/mtk_main.py'),'exec'),env)
 M=type('MainTail',(),{'dispatch':env['dispatch'],'close':staticmethod(method('mtkclient/Library/mtk_main.py','Main','close',env))})
 main=M();main.info=messages.append
 directory=OUTPUT;destination=directory/'cli.bin';destination.write_bytes(b'EXISTING CLI DUMP')
 main.args=NS(offset=None,length='8',step=None,filename=str(destination),partitionname='boot_a',parttype='user')
 try:main.dispatch(mtk,'r',None)
 finally:
  assert destination.read_bytes()==b'EXISTING CLI DUMP'
  assert not list(directory.glob('.*.tmp'))
  print(json.dumps({'old_dump_preserved':True,'usb_imported':any(k=='usb' or k.startswith('usb.') for k in sys.modules),'messages':messages,'poison':cdc._io_failure}))


def main():
 pins=source_pins()
 directory=OUTPUT/'fixtures';directory.mkdir();cases=[]
 def case(name,*a,**kw):
  row=run_case(directory,name,*a,**kw);cases.append(row);return row
 # Frozen17 controls, now with checked outcomes. Datatype remains deliberately unconstrained.
 case('valid-buffer',success=True)
 case('bad-data-magic-file',[header(8,magic=1),*terminal()],file=True)
 row=case('oversized-data',[header(16),b'ABCDEFGHIJKLMNOP',*terminal()]);assert len(row['events'])==1
 case('early-error-file',terminal(1)+terminal(),file=True)
 case('zero-data-length-file',[header(0),*terminal()],file=True)
 case('nonzero-terminal-buffer',[header(8),b'ABCDEFGH',*terminal(1)])
 case('bad-terminal-magic-file',[header(8),b'ABCDEFGH',*terminal(magic=1)],file=True)
 row=case('wrong-terminal-length',[header(8),b'ABCDEFGH',*terminal(length=8)]);assert row['remaining_scripted_reads']==1
 case('command-false-file',terminal(),file=True,fault='command-false')
 case('short-header-direct-file',terminal(),file=True,fault='direct-short-header')
 case('short-header-exact-usb',[b'SHORT',b''])
 case('four-byte-frame-ambiguity',terminal(1)+terminal(),length=4,file=True)
 case('datatype-unvalidated',[header(8,datatype=0xBAD),b'ABCDEFGH',*terminal()],success=True)
 for fault in ('write-short','write-raise','close'):case('writer-'+fault,file=True,fault=fault,poison=fault!='close')
 x,cdc,messages,threads,events,env,trace=create(terminal()+terminal(0xC0040050)+terminal())
 command_result=type(x).cmd_read_data(x,0,8)
 assert command_result is False and len(cdc.EP_IN.steps)==2
 cases.append({'case':'send-param-false-honored','returned_bool':False,'remaining_scripted_reads':2,'events':events})
 # Positive multiple frames, exact small chunking, existing and absent files.
 data=b'ABCDEFGHIJKLMNOPQRST'
 frames=[header(8),data[:6],data[6:8],header(12),data[8:14],data[14:],*terminal()]
 for file in (False,True):case('multi-frame-'+('file' if file else 'buffer'),frames,length=20,file=file,success=True,data=data,packet=6)
 case('new-file',success=True,file=True,existing=False)
 case('physical-split-success',[header(8)[:3],header(8)[3:],b'ABC',b'DEFGH',header(4)[:5],header(4)[5:],b'\x00\x00',b'\x00\x00'],file=True,success=True)
 case('write-bool',[header(8),*[bytes([65+i]) for i in range(8)],*terminal()],packet=1,file=True,fault='write-bool')
 case('write-none',file=True,fault='write-none')
 case('close-before',file=True,fault='close-before',poison=False)
 row=case('selected-length',length=100,selected=(1,8,8),success=True,file=True);assert row['command_calls'][0]['size']==8
 # Exact file buffering: a frame bigger than the read application's4MiB cap.
 data=b'Z'*(0x400000+8);chunks=[header(len(data))]
 chunks.extend(data[i:i+0x10000] for i in range(0,len(data),0x10000));chunks.extend(terminal())
 row=case('large-frame-bounded-file',chunks,length=len(data),file=True,success=True,data=data,packet=0x800000)
 assert row['write_sizes']==[0x400000,8]
 for fault in ('ack-minus-one','ack-false','ack-usb','query-none','query-zero','query-negative','query-bool','query-poison'):
  case(fault,file=True,fault=fault)
 for fault in ('direct-short-data','direct-overlong-data','direct-short-terminal','direct-short-status'):
  case(fault,file=True,fault=fault)
 for fault in ('flush','write-lying','replace','progress-done'):
  case(fault,file=True,fault=fault,poison=False)
 for fault in ('temp-open','fdopen','fchmod'):case(fault,file=True,fault=fault,poison=False)
 for kind in ('missing-parent','symlink','hardlink','directory','fifo','parent-symlink','dotdot'):
  case('path-'+kind,file=True,path_kind=kind,poison=False)
 case('storage-false',selected=False,file=True,poison=False)
 case('storage-zero-length',selected=(1,8,0),file=True,poison=False)
 case('storage-bool-length',selected=(1,8,True),file=True,poison=False)
 case('storage-malformed',selected=(1,8),file=True,poison=False)
 case('existing-poison',file=True,initial_poison=True)
 case('zero-four-byte-ambiguity',terminal()+terminal(),length=4,file=True)
 case('raw-magic-terminal',[header(8),b'ABCDEFGH',*terminal(MAGIC)],file=True)
 case('data-frame-three',[header(3),b'ABC',*terminal()],file=True)
 case('physical-partial-payload',[header(8),b'ABC',b''],file=True)
 # Original actual CLI caller-chain tail, not a fabricated exit-code shim.
 next_process=directory/'next-process'
 shell='set -euo pipefail\n'+shlex.join([sys.executable,str(Path(__file__).resolve()),'--source',str(SOURCE),'--out',str(OUTPUT/'cli-replay'),'--cli-child'])+'\n'+shlex.join([sys.executable,'-c','from pathlib import Path;Path('+repr(str(next_process))+').write_text("ran")'])
 cli=subprocess.run(['bash','-c',shell],capture_output=True,text=True,cwd=OUTPUT,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
 (directory/'cli.stdout').write_text(cli.stdout);(directory/'cli.stderr').write_text(cli.stderr)
 assert cli.returncode!=0 and not next_process.exists(),(cli.returncode,cli.stdout,cli.stderr)
 assert '"old_dump_preserved": true' in cli.stdout and '"usb_imported": false' in cli.stdout
 assert source_pins()==pins, 'source inputs changed during replay'
 assert not any(k=='usb' or k.startswith('usb.') or k=='mtkclient' or k.startswith('mtkclient.') for k in sys.modules)
 result={'status':'PASS','cases':cases,'case_count':len(cases),'cli':{'exit':cli.returncode,'next_process_ran':False,'old_dump_preserved':True},'source_inputs_unchanged':True,'source':str(SOURCE),'source_pins':pins,'usb_or_target_imports':False,'directory':str(directory),'scope':'Actual read/cmd/ack/status/send/USB methods plus actual direct CLI chain; storage/query and admitted initial read command modeled except cmd_read_data send_param-failure case. Real private files and synchronous writer fault boundaries. No device I/O or hardware compatibility proof.'}
 (OUTPUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({key:result[key] for key in ('status','case_count','cli','directory')}))

if __name__=='__main__':
 SOURCE, OUTPUT, child = configure_paths()
 fixture.configure_source(SOURCE)
 if child:cli_child()
 else:main()
