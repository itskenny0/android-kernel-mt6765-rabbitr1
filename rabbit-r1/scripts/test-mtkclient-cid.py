#!/usr/bin/env python3
"""Run actual parser/DA guard bodies with synthetic identity and connection I/O."""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import logging
import os
from pathlib import Path
from struct import pack, unpack
import sys
from types import SimpleNamespace as NS

CID = bytes.fromhex('1234567890abcdef1122334455667788')
OTHER = bytes.fromhex('8899aabbccddeeff0011223344556677')
CONTROLS = []


def extract(path, classes=(), functions=()):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ClassDef) and node.name in classes:
            node.bases = []
            node.keywords = []
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
    return nodes


def compile_nodes(nodes, namespace, path):
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(path), 'exec'), namespace)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    CONTROLS.append(name)


def load(source):
    handler_path = source / 'mtkclient/Library/DA/mtk_da_handler.py'
    handler = extract(handler_path, classes=('DaHandler',))[0]
    handler.body = [n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name in
                    ('_check_expected_emmc_cid', 'handle_da_cmds', 'connect', 'configure_da')]
    exists = {'state': False}
    fake_os = NS(path=NS(dirname=os.path.dirname, join=os.path.join,
                        exists=lambda p: exists['state']))
    ns = dict(DAmodes=NS(XFLASH=5), os=fake_os, sys=sys)
    compile_nodes([handler], ns, handler_path)
    xflash_path = source / 'mtkclient/Library/DA/xflash/xflash_lib.py'
    xflash = extract(xflash_path, classes=('DAXFlash',))[0]
    xflash.body = [n for n in xflash.body if isinstance(n, ast.FunctionDef) and n.name == 'get_emmc_info']
    emmc = extract(source / 'mtkclient/Library/DA/storage.py', classes=('EmmcInfo',))
    compile_nodes(emmc + [xflash], ns, xflash_path)
    ns['unpack'] = unpack
    # Execute the original final DA branch of Main.run, without its hardware
    # setup imports/constructor. Connect/configure/dispatch remain actual bodies.
    main_path = source / 'mtkclient/Library/mtk_main.py'
    main = extract(main_path, classes=('Main',))[0]
    run = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    branch = run.body[-1]
    assert isinstance(branch, ast.If)
    while len(branch.orelse) == 1 and isinstance(branch.orelse[0], ast.If):
        branch = branch.orelse[0]
    wrapper = ast.parse('def run_da_branch(self, mtk, directory, loglevel):\n pass\n').body[0]
    wrapper.body = branch.orelse
    compile_nodes([wrapper], ns, main_path)
    return ns, exists


def setup(ns, raw_cid=CID):
    events = []
    failure = {'poison': None, 'query': None, 'status': 0, 'raw': None, 'close': None}
    def check_io():
        events.append('check_io')
        if failure['poison'] is not None:
            raise failure['poison']
    def close(reset=False):
        events.append(('close', reset))
        if failure['close'] is not None:
            raise failure['close']
    cfg = NS(hwparam_path='.', internal_flash=False, target_config={'sbc': False},
             is_brom=False, loader=None, stock=False, generatekeys=False, preloader=None,
             cid=CID, hwparam=None)
    cdc = NS(check_io=check_io, connected=False, connect=lambda: events.append('connect') or False)
    owner = NS(config=cfg, serialportname=None, reinited=False,
               port=NS(cdc=cdc, close=close), preloader=NS(init=lambda **kw: events.append('preloader')))
    storage = NS(flashtype='emmc', emmc=NS(cid=CID))
    da = ns['DAXFlash']()
    da.cmd = NS(GET_EMMC_INFO=0x40001)
    da.config = cfg
    da.error = lambda *args: events.append('query_error')
    da.eh = NS(status=lambda value: str(value))
    def send(command):
        assert command == da.cmd.GET_EMMC_INFO
        events.append('fresh_query')
        if failure['query'] is not None:
            raise failure['query']
        if failure['raw'] is not None:
            return failure['raw']
        return bytearray(pack('<II8Q', 1, 512, 4194304, 4194304, 4194304,
                              0, 0, 0, 0, 125069950976) + raw_cid + pack('<Q', 0) + b'opaque08')
    da.send_devctrl = send
    da.status = lambda: events.append('terminal_status') or failure['status']
    owner.daloader = NS(flashmode=5, daconfig=NS(storage=storage), da=da, config=cfg,
                        is_patched=lambda: True, keys=lambda: events.append('keys'),
                        reinit=lambda: events.append('reinit'),
                        upload_da=lambda **kw: events.append('upload_da') or True,
                        writestate=lambda: events.append('writestate'))
    handler = object.__new__(ns['DaHandler'])
    handler.mtk = owner
    handler.config = cfg
    for name in ('info', 'debug', 'warning', 'error'):
        setattr(handler, name, lambda *args: None)
    handler.da_gpt = lambda **kw: events.append(('dispatch', 'gpt'))
    handler.da_read = lambda **kw: events.append(('dispatch', 'r'))
    handler.da_write = lambda **kw: events.append(('dispatch', 'w'))
    return handler, owner, events, failure


def arguments(cmd, expected=CID.hex()):
    return NS(cmd=cmd, expected_emmc_cid=expected, directory='fixture',
              partitionname='boot_a', filename='fixture/image', parttype='user',
              offset='0x0', length='0x200')


def run_guard(ns, label, alter, expected=CID.hex(), cmd='w', wanted_error=None):
    h, m, events, fault = setup(ns)
    alter(h, m, events, fault)
    try:
        h.handle_da_cmds(m, cmd, arguments(cmd, expected))
    except Exception as error:
        check(label, not any(isinstance(e, tuple) and e[0] == 'dispatch' for e in events)
              and ('close', False) in events and
              (wanted_error is None or error is wanted_error))
    else:
        raise AssertionError(label + ': admitted')
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = Path('/rabbitr1').resolve()
    source = args.source.resolve(strict=True)
    output = args.out.resolve()
    if (not source.is_relative_to(root) or source == root or
            not output.is_relative_to(root) or output == root or
            output.is_relative_to(source) or output.exists()):
        parser.error('workspace source and new separate workspace --out required')
    output.mkdir(parents=True)
    ns, exists = load(source)
    for cmd in ('gpt', 'r', 'w'):
        h, m, events, failure = setup(ns)
        m.config.generatekeys = True
        h.handle_da_cmds(m, cmd, arguments(cmd))
        check('fresh bytearray CID and opaque tail before ' + cmd,
              events == ['check_io', 'fresh_query', 'terminal_status', 'check_io',
                         'keys', ('dispatch', cmd)])
        h, m, events, failure = setup(ns)
        m.daloader.da.get_emmc_info = lambda **kw: NS(type=1, block_size=512,
                                                    user_size=4096, cid=CID)
        h.handle_da_cmds(m, cmd, arguments(cmd, CID.hex().upper()))
        check('bytes CID and case-normalized expected ' + cmd, ('dispatch', cmd) in events)
        h, m, events, failure = setup(ns, OTHER)
        h.handle_da_cmds(m, cmd, arguments(cmd, None))
        check('unguarded old dispatch remains ' + cmd,
              events == [('dispatch', cmd)])
    run_guard(ns, 'fresh other CID rejects despite matching cached CID/state',
              lambda h,m,e,f: setattr(m.daloader.da, 'send_devctrl', lambda c:
                  pack('<II8Q',1,512,0,0,0,0,0,0,0,4096)+OTHER+pack('<Q',0)))
    h,m,e,f=setup(ns);m.config.cid=OTHER;m.daloader.daconfig.storage.emmc.cid=OTHER
    h.handle_da_cmds(m,'w',arguments('w'))
    check('fresh match does not trust stale cached mismatch', ('dispatch','w') in e)
    for mode in (3,6):
        e=run_guard(ns, 'unsupported DA mode ' + str(mode),
                    lambda h,m,e,f,v=mode:setattr(m.daloader,'flashmode',v))
        assert 'fresh_query' not in e
    e=run_guard(ns,'non-eMMC denied',lambda h,m,e,f:setattr(m.daloader.daconfig.storage,'flashtype','ufs'))
    assert 'fresh_query' not in e
    run_guard(ns,'no query response',lambda h,m,e,f:f.update(raw=b''))
    for status in (1,0xC0010004,-1):
        run_guard(ns,'nonzero query terminal '+str(status),lambda h,m,e,f,v=status:f.update(status=v))
    run_guard(ns,'short metadata',lambda h,m,e,f:f.update(raw=b'bad'))
    err=IOError('synthetic query failure')
    run_guard(ns,'query exception identity retained',lambda h,m,e,f:f.update(query=err),wanted_error=err)
    err=IOError('synthetic poison')
    run_guard(ns,'preexisting poison retained',lambda h,m,e,f:f.update(poison=err),wanted_error=err)
    def swallowed(h,m,e,f):
        def query(**kw):
            f['poison']=err
            return None
        m.daloader.da.get_emmc_info=query
    run_guard(ns,'swallowed query transport poison retained',swallowed,wanted_error=err)
    for field,value in [('type',0),('type',True),('type',-1),('block_size',4096),
                        ('block_size',True),('user_size',0),('user_size',513),
                        ('user_size',False),('cid','not-bytes'),('cid',CID[:-1]),
                        ('cid',CID+b'x'),('cid',bytes(16)),('cid',b'\xff'*16)]:
        def alter(h,m,e,f,field=field,value=value):
            data=dict(type=1,block_size=512,user_size=4096,cid=CID)
            data[field]=value;m.daloader.da.get_emmc_info=lambda **kw:NS(**data)
        run_guard(ns,'invalid identity field '+field+'/'+repr(value),alter)
    run_guard(ns,'different dispatch owner',lambda h,m,e,f:setattr(h,'mtk',NS()))
    run_guard(ns,'unsupported guarded command',lambda *args:None,cmd='e')
    err=IOError('primary query error')
    run_guard(ns,'cleanup failure retains primary error',
              lambda h,m,e,f:f.update(query=err,close=IOError('cleanup')),wanted_error=err)
    for value in ('',CID.hex()[:-1],CID.hex()+'0','g'*32,'0'*32,'f'*32,
                  ' '+CID.hex()[1:],True):
        e=run_guard(ns,'invalid direct expected '+repr(value),lambda *args:None,expected=value)
        assert 'fresh_query' not in e
    # Actual Main final branch + actual connect/configure paths with only the
    # preloader/upload/reinit endpoints modeled. No-state and .state differ.
    Handler=ns['DaHandler']
    for state in (False,True):
        exists['state']=state
        ns['DaHandler']=Handler
        h,m,e,f=setup(ns)
        m.port.cdc.connect=lambda:e.append('connect') or state
        ns['DaHandler']=lambda connected,level:h
        main=NS(args=arguments('w'),info=lambda *args:None,close=lambda:e.append('exit0'))
        ns['cmd']='w'
        ns['run_da_branch'](main,m,'fixture',0)
        required=['connect','reinit'] if state else ['connect','preloader','upload_da','writestate']
        check(('state reinit' if state else 'new DA')+' precedes fresh check and dispatch',
              e[:len(required)]==required and e.index('fresh_query')>len(required)-1 and
              e.index(('dispatch','w'))>e.index('terminal_status') and e[-1]=='exit0')
        ns['DaHandler']=Handler
        h,m,e,f=setup(ns,OTHER);m.port.cdc.connect=lambda:e.append('connect') or state
        ns['DaHandler']=lambda connected,level:h
        try:ns['run_da_branch'](main,m,'fixture',0)
        except ValueError:
            check(('state' if state else 'new DA')+' mismatch escapes actual Main branch',
                  'exit0' not in e and not any(isinstance(x,tuple) and x[0]=='dispatch' for x in e))
        else:raise AssertionError('Main swallowed mismatch')
    ns['DaHandler']=Handler
    # Actual mtk.py parser/function bodies, replacing only Main construction.
    path=source/'mtk.py';tree=ast.parse(path.read_text());seen=[]
    pn=dict(argparse=argparse,logging=logging,sys=sys,os=os,metamodes='off',
            Main=lambda args:NS(run=lambda parser:seen.append(args) or args))
    kept=[n for n in tree.body if isinstance(n,ast.FunctionDef) or
          isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('INFO','CMDS_HELP') for t in n.targets)]
    compile_nodes(kept,pn,path)
    for cmd,tail in [('gpt',['fixture']),('r',['boot_a','file']),('w',['boot_a','file'])]:
        old=sys.argv;sys.argv=['mtk.py',cmd,*tail,'--expected-emmc-cid',CID.hex().upper()]
        try:result=pn['main']()
        finally:sys.argv=old
        check('actual CLI accepts '+cmd, result.expected_emmc_cid==CID.hex())
    for argv in [ ['e','boot_a','--expected-emmc-cid',CID.hex()],
                  ['--expected-emmc-cid',CID.hex(),'gpt','fixture'],
                  ['w','boot_a','file','--expected-emmc-cid','0'*32],
                  ['w','boot_a','file','--expected-emmc-cid','1 '*16],
                  ['w','boot_a','file','--expected-emmc-cid','f'*32] ]:
        count=len(seen);old=sys.argv;sys.argv=['mtk.py',*argv]
        try:
            with contextlib.redirect_stderr(io.StringIO()):pn['main']()
        except SystemExit as error:check('CLI rejects before Main '+repr(argv[0]),error.code==2 and len(seen)==count)
        else:raise AssertionError('invalid CLI admitted')
        finally:sys.argv=old
    sources={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
             (source/'mtk.py',source/'mtkclient/Library/DA/mtk_da_handler.py',
              source/'mtkclient/Library/DA/xflash/xflash_lib.py',
              source/'mtkclient/Library/DA/storage.py',source/'mtkclient/Library/mtk_main.py')}
    result={'status':'pass','controls':CONTROLS,'count':len(CONTROLS),'sources':sources,
            'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'scope':'Actual extracted CLI/parser/query/guard/dispatch/connection bodies; synthetic endpoints only; no USB imports or discovery'}
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS',len(CONTROLS),'CID controls; no USB import/discovery')


if __name__=='__main__':
    main()
