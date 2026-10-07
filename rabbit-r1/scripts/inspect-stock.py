#!/usr/bin/env python3
"""Extract only the stock boot artifacts, check table bounds, and record facts."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import zipfile
import zlib

ROOT = Path('/rabbitr1')
os.chdir(ROOT)
os.environ.update(TMPDIR=str(ROOT/'.tmp'), PYTHONDONTWRITEBYTECODE='1')
stock = ROOT/'firmware/stock-v0.8.293'
research = ROOT/'docs/research'
stock.mkdir(parents=True,exist_ok=True)
research.mkdir(parents=True,exist_ok=True)
archive = ROOT/'downloads/rabbit_OS_v0.8.293.zip'
expected = json.loads((ROOT/'sources.lock.json').read_text())['archives'][archive.name]['sha256']
with archive.open('rb') as f:
    actual = hashlib.file_digest(f,'sha256').hexdigest()
if actual != expected:
    raise SystemExit('Stock archive checksum mismatch')
keep = {'boot.img','dtbo.img','vbmeta.img','vbmeta_vendor.img','vbmeta_system.img',
        'lk.img','logo.bin','MT6765_Android_scatter.txt','MT6765_Android_scatter.xml'}
with zipfile.ZipFile(archive) as z:
    for info in z.infolist():
        name = Path(info.filename).name
        if name in keep and not info.filename.startswith('__MACOSX/'):
            (stock/name).write_bytes(z.read(info))
(research/'lk-strings.txt').write_text(subprocess.check_output(['strings',str(stock/'lk.img')],text=True))
unpacked = stock/'boot-unpacked'
header = subprocess.check_output(['python3',str(ROOT/'src/mkbootimg/unpack_bootimg.py'),
    '--boot_img',str(stock/'boot.img'),'--out',str(unpacked)],text=True)
(research/'stock-boot-header.txt').write_text('\n'.join(line.rstrip() for line in header.splitlines())+'\n')
image = zlib.decompress((unpacked/'kernel').read_bytes(),31)
(unpacked/'Image').write_bytes(image)
begin = image.index(b'IKCFG_ST')+8
end = image.index(b'IKCFG_ED',begin)
(research/'stock.config').write_bytes(zlib.decompress(image[begin:end],31))

def table_entries(data):
    magic, total, hsize, esize, count, offset, pagesize, version = struct.unpack_from('>8I',data)
    if magic != 0xd7b7ab1e or total > len(data) or hsize < 32 or esize < 32 or offset < hsize or offset+count*esize > total:
        raise ValueError('Invalid Android DT table')
    entries=[]
    for index in range(count):
        size, start, ident, rev, *custom = struct.unpack_from('>8I',data,offset+index*esize)
        if start < offset+count*esize or start+size > total:
            raise ValueError('DT table entry out of bounds')
        blob = data[start:start+size]
        if blob[:4] != b'\xd0\x0d\xfe\xed':
            raise ValueError('Entry is not an FDT')
        entries.append((blob,{'index':index,'size':size,'offset':start,'id':ident,'rev':rev,'custom':custom}))
    return entries

base = table_entries((unpacked/'dtb').read_bytes())
overlays = table_entries((stock/'dtbo.img').read_bytes())
if len(base) != 1 or len(overlays) != 1:
    raise SystemExit('Unexpected DT table count; inspect before applying overlays')
(unpacked/'base.dtb').write_bytes(base[0][0])
for blob, entry in overlays:
    (stock/f'overlay-{entry["index"]}.dtbo').write_bytes(blob)
dtc = ROOT/'out/mainline/scripts/dtc/dtc'
overlay_tool = ROOT/'out/mainline/scripts/dtc/fdtoverlay'
with (ROOT/'logs/stock-dtc.log').open('w') as log:
    subprocess.run([str(overlay_tool),'-i',str(unpacked/'base.dtb'),'-o',str(stock/'merged.dtb'),str(stock/'overlay-0.dtbo')],check=True,stderr=log)
    for source, name in [(unpacked/'base.dtb','stock-base.dts'),(stock/'overlay-0.dtbo','stock-overlay.dts'),(stock/'merged.dtb','stock-merged.dts')]:
        subprocess.run([str(dtc),'-I','dtb','-O','dts','-o',str(research/name),str(source)],check=True,stderr=log)
partitions=[]
for block in (stock/'MT6765_Android_scatter.txt').read_text().split('- partition_index:')[1:]:
    fields=dict(re.findall(r'^\s+(\w+):\s*(.*?)\s*$',block,re.M))
    partitions.append({key:fields[key] for key in ['partition_name','file_name','linear_start_addr','partition_size','is_download'] if key in fields})
facts={'firmware':'RabbitOS v0.8.293','archive_sha256':actual,
       'kernel_version':re.search(rb'Linux version [^\0]+',image).group().decode().strip(),
       'base_dt_entries':[e for _,e in base],'overlay_entries':[e for _,e in overlays],
       'partitions':partitions,'note':'Static firmware data; loader-time RAM/carve-out changes are not captured.'}
(research/'stock-facts.json').write_text(json.dumps(facts,indent=2)+'\n')
print(header)
print('Stock metadata:',research/'stock-facts.json')
