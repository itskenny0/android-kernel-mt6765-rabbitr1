#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Preserved instruction oracle; requires verified stock firmware and Unicorn.
from pathlib import Path
import hashlib, importlib.util, json, re, struct
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X30, UC_ARM64_REG_SP, UC_ARM64_REG_PC
ROOT=Path('/rabbitr1'); OUT=ROOT/'out/battery-review'
spec=importlib.util.spec_from_file_location('validate',ROOT/'scripts/validate-kernel.py')
validate=importlib.util.module_from_spec(spec); spec.loader.exec_module(validate)
dtb=(ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes()
assert hashlib.sha256(dtb).hexdigest()=='e2b20d7cfc48bbf4498e8fd12610328eac8d2f6887d19d2c1df149e3145c87f4'
node=validate.fdt_nodes(dtb)['/pwrap@1000d000/main_pmic/mtk_gauge']
def cells(key):return list(struct.unpack('>'+str(len(node[key])//4)+'i',node[key]))
temps=[cells('TEMPERATURE_T'+str(i))[0] for i in range(4)]
profiles=[]
for i in range(4):
    row=cells('battery0_profile_t'+str(i)); profiles.append([row[j:j+3] for j in range(0,len(row),3)])
    assert len(profiles[-1])==cells('battery0_profile_t'+str(i)+'_num')[0]==53
header=(ROOT/'src/kernel/drivers/power/supply/mtk_battery_table.h').read_text()
def qarray(name):
    content=re.search(r'int '+name+r'\[MAX_TABLE\]\[TOTAL_BATTERY_NUMBER\] = \{(.*?)\n};',header,re.S)[1]
    return [int(row[0]) for row in re.findall(r'\{\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\}',content)][:4]
qmax=qarray('g_Q_MAX'); qhigh=qarray('g_Q_MAX_H_CURRENT')
ORIG=0xffffff8008ad6000; SIZE=0x9000; BASE=0x100000; GM=0x200000; STACK=0x308000; STOP=0x400000
elf=(ROOT/'out/stock-kernel.elf').read_bytes(); assert hashlib.sha256(elf).hexdigest()=='a03b5ff0b811de4c3d2b8c971b213b36274106536191f6588b2fca7372d4be75'; shoff=struct.unpack_from('<Q',elf,40)[0]; shentsize,shnum=struct.unpack_from('<HH',elf,58)
for i in range(shnum):
    sec=struct.unpack_from('<IIQQQQIIQQ',elf,shoff+i*shentsize); va,offset,size=sec[3:6]
    if va<=ORIG and ORIG+SIZE<=va+size: code=elf[offset+ORIG-va:offset+ORIG-va+SIZE];break
else:raise AssertionError('Missing kernel text')
symbols={}
all_symbols=[]
for line in (ROOT/'out/stock-kernel-symbols.txt').read_text().splitlines():
    fields=line.split()
    if len(fields)==3:
        address=int(fields[0],16); symbols[fields[2]]=address; all_symbols.append((address,fields[2]))
uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
for start,size in [(BASE,SIZE),(GM,0x10000),(0x300000,0x10000),(STOP,0x1000)]:uc.mem_map(start,size)
uc.mem_write(BASE,code)
car_fixture=0
canary=0xffffff800973db08-ORIG+BASE;uc.mem_map(canary&~0xfff,0x1000);uc.mem_write(canary,bytes(8))
def hook(uc,pc,size,_):
    insn=struct.unpack_from('<I',code,pc-BASE)[0]
    if insn>>26!=0x25:return
    imm=insn&0x3ffffff
    if imm&0x2000000:imm-=0x4000000
    target=ORIG+pc-BASE+imm*4
    if target==symbols['gauge_get_int_property']:
        assert uc.reg_read(UC_ARM64_REG_X0)==2
        uc.reg_write(UC_ARM64_REG_X0,car_fixture&0xffffffff)
    elif target==symbols['bat_get_debug_level']:uc.reg_write(UC_ARM64_REG_X0,0)
    elif target==symbols['_mcount']:pass
    elif ORIG<=target<ORIG+SIZE:return
    else:raise AssertionError(('Unexpected external call',hex(target),hex(ORIG+pc-BASE)))
    uc.reg_write(UC_ARM64_REG_PC,pc+4)
uc.hook_add(UC_HOOK_CODE,hook,begin=BASE,end=BASE+SIZE-1)
def put(offset,value):uc.mem_write(GM+offset,struct.pack('<I',value&0xffffffff))
def get(offset):return struct.unpack('<i',uc.mem_read(GM+offset,4))[0]
def read_profile():return [struct.unpack('<IHHHH',uc.mem_read(GM+16784+i*12,12)) for i in range(100)]
calls={}
def call(name,*args):
    for reg,val in zip((UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2,UC_ARM64_REG_X3),args):uc.reg_write(reg,val&0xffffffffffffffff)
    uc.reg_write(UC_ARM64_REG_X30,STOP);uc.reg_write(UC_ARM64_REG_SP,STACK)
    uc.emu_start(symbols[name]-ORIG+BASE,STOP,count=100000)
    assert uc.reg_read(UC_ARM64_REG_PC)==STOP,(name,hex(uc.reg_read(UC_ARM64_REG_PC)))
    calls[name]=calls.get(name,0)+1
    value=uc.reg_read(UC_ARM64_REG_X0)&0xffffffff
    return value-(1<<32) if value&(1<<31) else value

def init(temp):
    uc.mem_write(GM,bytes(0x10000));put(3164,4);put(15568,255);put(16776,254);put(524,temp)
    put(644,10000);put(652,100);put(2584,100);put(2576,75)
    for i,rows in enumerate(profiles):
        off=3168+i*1240
        for j,value in enumerate((temps[i],qmax[i],qhigh[i],0,10000,33500,5000,33500,0,53)):put(off+j*4,value)
        for j,row in enumerate(rows+[rows[-1]]*(100-len(rows))):
            mah,v,r=row; uc.mem_write(GM+off+40+j*12,struct.pack('<IHHHH',mah,v,r,r,0))
    call('fgr_construct_battery_profile',GM,254)

def trunc(n,d):return (-1 if (n<0)!=(d<0) else 1)*(abs(n)//abs(d))
def interp(i1,b1,i2,b2,i):return b1+trunc((b2-b1)*(i-i1),i2-i1)
def expected_profile(t):
    t=max(-10,min(50,t))
    i=next((i for i in range(1,4) if t>=temps[i]),3)
    return [[interp(temps[i],lo[k],temps[i-1],hi[k],t) for k in range(3)] for lo,hi in zip(profiles[i],profiles[i-1])]
profile_cases=[]
for t in range(-20,61):
    init(t); got=read_profile(); want=expected_profile(t)
    assert [list(row[:3]) for row in got[:53]]==want
    assert all(row[:4]==(0,0,0,0) for row in got[53:])
    clamped=max(-10,min(50,t)); i=next((i for i in range(1,4) if clamped>=temps[i]),3)
    assert get(632)==interp(temps[i],qmax[i]*10,temps[i-1],qmax[i-1]*10,clamped)
    assert get(636)==interp(temps[i],qhigh[i]*10,temps[i-1],qhigh[i-1]*10,clamped)
    profile_cases.append({'temp_c':t,'qmax_01mah':get(632),'qmax_high_01mah':get(636),'tail_zero':True,'profile':want})
assert call('fgauge_get_profile_id')==0
math_vectors=[]
for t in [-20,-10,-5,0,12,25,37,50,60]:
    init(t); constructed=read_profile()
    call('fg_construct_battery_profile_by_qmax',GM,get(632),254)
    rows=read_profile()[:53]
    for soc in list(range(-100,10101,100)):
        dod=10000-soc
        i=next((i for i,r in enumerate(rows) if r[4]>=dod),len(rows))
        want=rows[0][1] if i==0 else rows[-1][1] if i==len(rows) else interp(rows[i-1][4],rows[i-1][1],rows[i][4],rows[i][1],dod)
        got=call('SOC_to_OCV_c',GM,soc); assert got==want
        assert call('DOD_to_OCV_c',GM,dod)==want
        math_vectors.append([t,'soc_to_ocv',soc,got])
    for ocv in sorted({v for r in rows for v in (r[1]-1,r[1],r[1]+1)}|{33000,34000,35000,40000,44000,45000}):
        i=next((i for i,r in enumerate(rows) if r[1]<=ocv),len(rows))
        dod=rows[0][4] if i==0 else rows[-1][4] if i==len(rows) else interp(rows[i-1][1],rows[i-1][4],rows[i][1],rows[i][4],ocv)
        got=call('OCV_to_SOC_c',GM,ocv);assert got==10000-dod
        assert call('OCV_to_DOD_c',GM,ocv)==dod
        math_vectors.append([t,'ocv_to_soc',ocv,got])

tail_cases=[]
for t in [-10,0,25,50]:
    for padding in ['zero','repeat_last']:
        init(t)
        if padding=='repeat_last':
            last=bytes(uc.mem_read(GM+16784+52*12,12))
            for i in range(53,100):uc.mem_write(GM+16784+i*12,last)
        call('fg_construct_battery_profile_by_vboot',GM,33500,254)
        initial=get(596)
        vboot=call('fg_compensate_battery_voltage_from_low',GM,33500,-5000,254)
        call('fg_construct_battery_profile_by_vboot',GM,vboot,254)
        final=get(596)
        tail_cases.append(dict(temp_c=t,padding=padding,rac_fixture_mohm=0,initial_qmax_01mah=initial,vboot_01mv=vboot,final_qmax_01mah=final,profile_first=read_profile()[0],profile_last=read_profile()[52]))

live_counter_vectors=[]
for t in [-10,0,25,50]:
    init(t); quse=get(632)
    call('fg_construct_battery_profile_by_qmax',GM,quse,254)
    for d0_ocv in range(34000,44001,1000):
        put(544,d0_ocv)
        initial=call('OCV_to_DOD_c',GM,d0_ocv)
        for car_fixture in [-12000,-10000,-1,0,1,10000,12000]:
            call('fgr_update_c_dod',GM)
            want=initial-trunc(car_fixture*10000,quse)
            assert get(548)==initial and get(612)==quse
            assert get(556)==want and get(560)==10000-want
            live_counter_vectors.append([t,d0_ocv,quse,car_fixture,get(556),get(560)])

hashes={}
for name in calls:
    start=symbols[name]; end=min(addr for addr,n in all_symbols if addr>start)
    hashes[name]={'address':hex(start),'size':end-start,'sha256':hashlib.sha256(code[start-ORIG:end-ORIG]).hexdigest()}
result={'hardware_tested':False,'calls':calls,'stock_kernel_elf_sha256':hashlib.sha256(elf).hexdigest(),'stock_dtb_sha256':hashlib.sha256(dtb).hexdigest(),'function_hashes':hashes,'temperature_profiles':profile_cases,'ocv_vectors':math_vectors,'tail_cases':tail_cases,'live_counter_vector_columns':['temp_c','seed_ocv_01mv','quse_01mah','car_01mah','dod_001percent','soc_001percent'],'live_counter_vectors':live_counter_vectors,'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'src/kernel/drivers/power/supply/mtk_battery_algo.c',ROOT/'src/kernel/drivers/power/supply/mtk_battery_table.h',ROOT/'src/kernel/drivers/power/supply/mtk_battery.h']},'scope':'Actual stock ARM64 profile lookup/construction, Qmax and OCV arithmetic. Logging and instrumentation external calls are mocked; live-DOD cases additionally supply only gauge_get_int_property(COULOMB) from an explicit fixture. Primary input tables are parsed from verified stock DTB. Qmax-by-Qmax cases intentionally exercise a pure conversion with known Qmax; they are not claimed to be the configured full estimator (QMAX_SEL=1). Tail comparison includes a deliberate alternate fixture to expose dependence on unconstructed entries.'}
(OUT/'stock-soc-math-replay.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'calls':calls,'tail_cases':tail_cases,'hardware_tested':False},indent=2))

# Capture normalization and repeated-coordinate boundaries directly from stock.
# These are explicit supplied-capacity fixtures, not measured usable capacity.
normalization_vectors = []
repeated_dod_vectors = []
for t in [-10, 0, 25, 50]:
    init(t)
    capacities = [get(632), get(636)]
    for capacity in capacities:
        init(t)
        call('fg_construct_battery_profile_by_qmax', GM, capacity, 254)
        rows = read_profile()[:53]
        normalization_vectors.append([t, capacity, [row[4] for row in rows]])
        boundaries = {0, 10000, rows[-1][4]}
        for i in range(1, len(rows)):
            if rows[i][4] == rows[i-1][4]:
                boundaries.add(rows[i][4])
        for dod in sorted({edge + step for edge in boundaries for step in [-1, 0, 1]}):
            repeated_dod_vectors.append([t, capacity, dod, call('DOD_to_OCV_c', GM, dod)])
extra = {'stock_kernel_elf_sha256': hashlib.sha256(elf).hexdigest(),
         'stock_dtb_sha256': hashlib.sha256(dtb).hexdigest(),
         'hardware_tested': False, 'normalization_vectors': normalization_vectors,
         'repeated_dod_vectors': repeated_dod_vectors}
(ROOT/'out/battery-review/stock-soc-boundaries.json').write_text(json.dumps(extra, indent=2)+'\n')
print('Captured', len(normalization_vectors), 'normalizations and',
      len(repeated_dod_vectors), 'first-crossing boundaries from stock instructions')
