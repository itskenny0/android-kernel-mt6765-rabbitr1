#!/usr/bin/env python3
from pathlib import Path
import argparse,hashlib,json,re,shlex,shutil,subprocess,tempfile
HERE=Path(__file__).resolve().parent
SOURCE=HERE.parent/'initramfs/r1-log-shutdown'
PRE=r'''
cat() {
 if [ "$CASE" = boot_id_failure ] && [ "$1" = "$FAKE/proc/sys/kernel/random/boot_id" ]; then return 1; fi
 command cat "$@"
}
uname() { printf '%s\n' '7.1.0-rabbit-r1-bringup+'; }
sleep() {
 printf 'sleep %s\n' "$*" >> "$FAKE/events"
 [ "$CASE" != wait_failure ]
}
expdb_checkpoint() {
 printf 'verify %s|%s|%s\n' "$1" "$2" "$3" >> "$FAKE/events"
 [ "$CASE" != verify_failure ]
}
busybox() {
 case "$1" in
 rmmod)
  [ "$#" = 2 ] && [ "$2" = pstore_blk ] || exit 90
  [ "$(command cat "$FAKE/dev/kmsg")" = '<6>r1: expdb shutdown checkpoint 12345678-1234-4abc-9def-123456789abc 7.1.0-rabbit-r1-bringup+' ] || exit 93
  printf 'unload\n' >> "$FAKE/events"
  [ "$CASE" != unload_failure ] || return 1
  [ "$CASE" = unload_still_present ] || rmdir "$FAKE/sys/module/pstore_blk"
  ;;
 poweroff)
  [ "$#" = 2 ] && [ "$2" = -f ] || exit 91
  printf 'poweroff\n' >> "$FAKE/events"
  [ "$CASE" != poweroff_failure ] || return 1
  exit 42
  ;;
 *) exit 92;;
 esac
}
'''
cases={'success':42,'disabled':0,'exact_opt_in':0,'no_expdb':1,'wait_failure':1,
 'no_logger':1,'no_module':1,'no_device':1,'boot_id_failure':1,'marker_write_failure':1,
 'unload_failure':1,'unload_still_present':1,'verify_failure':1,'poweroff_failure':1}
rows=[]
source=SOURCE.read_text()
shells=[['/bin/sh']]
if shutil.which('busybox'):shells.append([shutil.which('busybox'),'sh'])
parser=argparse.ArgumentParser(description='Exercise the diagnostic shutdown policy without device operations.')
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args();out=args.out.resolve()
assert out.is_relative_to(Path('/rabbitr1'))
out.mkdir(parents=True,exist_ok=False)
for shell in shells:
 subprocess.run([*shell,'-n',str(SOURCE)],check=True)
 with tempfile.TemporaryDirectory(dir=out) as td:
  for case,code in cases.items():
   fake=Path(td)/case
   for path in ('run','dev','sys/module','proc/sys/kernel/random'):(fake/path).mkdir(parents=True,exist_ok=True)
   cmd='r1.expdb=1 r1.log_shutdown=1'
   if case=='disabled':cmd='r1.expdb=1'
   if case=='exact_opt_in':cmd='r1.expdb=1 r1.log_shutdown=10'
   if case=='no_expdb':cmd='r1.log_shutdown=1'
   (fake/'proc/cmdline').write_text(cmd+'\n');(fake/'proc/sys/kernel/random/boot_id').write_text('12345678-1234-4abc-9def-123456789abc\n')
   if case=='marker_write_failure':(fake/'dev/kmsg').mkdir()
   if case!='no_module':(fake/'sys/module/pstore_blk').mkdir()
   if case!='no_logger':(fake/'run/expdb-logger').write_text('ready\n')
   if case!='no_device':(fake/'run/expdb-device').write_text('/dev/dm-0\n')
   body=source.replace('/bin/busybox','busybox').replace('expdb-checkpoint ', 'expdb_checkpoint ')
   body=re.sub(r'/run/|/dev/kmsg|/sys/|/proc/',lambda m:str(fake)+m[0],body)
   script=fake/'run.sh';script.write_text('FAKE='+shlex.quote(str(fake))+'\nCASE='+shlex.quote(case)+'\n'+PRE+body)
   r=subprocess.run([*shell,str(script)],capture_output=True,text=True,timeout=5)
   assert r.returncode==code,(case,r.stdout,r.stderr)
   events=(fake/'events').read_text().splitlines() if (fake/'events').exists() else []
   assert ('poweroff' in events)==(case in ('success','poweroff_failure')), (case,events)
   if 'poweroff' in events:
    assert events==['sleep 60','unload','verify /dev/dm-0|12345678-1234-4abc-9def-123456789abc|7.1.0-rabbit-r1-bringup+','poweroff']
   if case in ('disabled','exact_opt_in','no_expdb'):assert events==[]
   if case not in ('success','disabled','exact_opt_in'):
    assert (fake/'run/expdb-shutdown.status').read_text().startswith('failed:')
   rows.append({'shell':shell,'case':case,'exit':r.returncode,'events':events})
result={'passed':True,'controls':len(rows),'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'scope':'Actual shell with modeled logger, storage verifier and poweroff; no real device or poweroff syscall.','rows':rows}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='rows'}))
