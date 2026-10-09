#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
import contextlib,gzip,hashlib,importlib.util,io,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
CANDIDATE=HERE.parent
CANON=CANDIDATE
sys.path.insert(0,str(CANON/'scripts'))
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
mode=load('health_mode',CANDIDATE/'scripts/r1-health-mode.py')
sha=lambda b:hashlib.sha256(b).hexdigest()
def kernel(config):
 image=b'fixture Image\0IKCFG_ST'+gzip.compress(config,mtime=0)+b'IKCFG_ED\0end'
 files={'config':config,'Image':image,'Image.gz':gzip.compress(image,mtime=0)}
 record={'source_commit':'0'*40,'modules':{},'config_sha256':sha(config),'artifacts':{n:{'bytes':len(b),'sha256':sha(b)} for n,b in files.items()}}
 return record,files
OFF=b'CONFIG_IKCONFIG=y\nCONFIG_BATTERY_MT6357=y\n# CONFIG_BATTERY_MT6357_R1_SOC is not set\n'
ON=OFF.replace(b'# CONFIG_BATTERY_MT6357_R1_SOC is not set',b'CONFIG_BATTERY_MT6357_R1_SOC=y')
def validate(product,record,files):return mode.validate(product,record,files['Image.gz'],files['Image'],files['config'])
class ModeTests(unittest.TestCase):
 def test_explicit_product_config_pair(self):
  for config,selected,other in [(OFF,'lineage_r1','lineage_r1_soc'),(ON,'lineage_r1_soc','lineage_r1')]:
   record,files=kernel(config);attestation=validate(selected,record,files)
   self.assertEqual(attestation['experimental_soc_health'],selected.endswith('_soc'))
   with self.assertRaisesRegex(ValueError,'Product/kernel SOC mismatch'):validate(other,record,files)
 def test_actual_production_prebuilt_is_default_off(self):
  dist=Path('/rabbitr1/dist/mainline');record=json.loads((dist/'build.json').read_bytes())
  files={name:(dist/name).read_bytes() for name in ['Image','Image.gz','config']}
  self.assertFalse(validate('lineage_r1',record,files)['experimental_soc_health'])
  with self.assertRaisesRegex(ValueError,'Product/kernel SOC mismatch'):validate('lineage_r1_soc',record,files)
 def test_record_and_embedded_config_disagreement(self):
  record,files=kernel(ON);files['config']=OFF
  record['artifacts']['config']={'bytes':len(OFF),'sha256':sha(OFF)};record['config_sha256']=sha(OFF)
  with self.assertRaisesRegex(ValueError,'Embedded kernel config differs'):validate('lineage_r1',record,files)
  record,files=kernel(ON);record['artifacts']['Image.gz']['sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'record mismatch'):validate('lineage_r1_soc',record,files)
 def test_missing_duplicate_or_invalid_soc_values(self):
  for config,pattern in [(ON.replace(b'_SOC=y',b'_SOC=m'),'SOC must'),(ON+b'CONFIG_BATTERY_MT6357_R1_SOC=n\n','Duplicate'),(ON.replace(b'CONFIG_BATTERY_MT6357=y',b'CONFIG_BATTERY_MT6357=m'),'gauge built in')]:
   with self.subTest(config=config):
    record,files=kernel(config)
    with self.assertRaisesRegex(ValueError,pattern):validate('lineage_r1_soc',record,files)
  old=OFF.replace(b'# CONFIG_BATTERY_MT6357_R1_SOC is not set\n',b'')
  record,files=kernel(old);self.assertEqual(validate('lineage_r1',record,files)['soc_config'],'n')
 def test_gzip_bound_trailer_and_missing_ikconfig(self):
  for image in [b'missing IKCONFIG',b'IKCFG_ST'+gzip.compress(ON)+b'bad end',b'IKCFG_ST'+gzip.compress(ON)+b'IKCFG_EDIKCFG_ST']:
   record,files=kernel(ON);files['Image']=image;files['Image.gz']=gzip.compress(image)
   for name in ['Image','Image.gz']:record['artifacts'][name]={'bytes':len(files[name]),'sha256':sha(files[name])}
   with self.assertRaises(ValueError):validate('lineage_r1_soc',record,files)
  with self.assertRaisesRegex(ValueError,'oversized'):mode.gzip_member(gzip.compress(b'A'*1000),99)
 def test_actual_product_fragment_selects_and_rejects(self):
  with tempfile.TemporaryDirectory(dir=Path('/rabbitr1/.tmp'),prefix='make-') as temp:
   root=Path(temp);staged=root/'device/rabbit/r1/prebuilt';staged.mkdir(parents=True)
   make=root/'check.mk';make.write_text('include '+str(CANDIDATE/'android/device/health/product.mk')+'\n$(info HAL=$(TARGET_HEALTH_HAL))\n$(info PACKAGES=$(PRODUCT_PACKAGES))\nall:\n\t@:\n')
   for config,product in [(OFF,'lineage_r1'),(ON,'lineage_r1_soc')]:
    record,files=kernel(config);(staged/'health-prebuilt.mk').write_bytes(mode.makefile(validate(product,record,files)))
    result=subprocess.run(['make','--no-print-directory','-f',str(make),'TARGET_PRODUCT='+product],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    self.assertEqual(result.returncode,0,result.stdout)
    self.assertIn('HAL='+('r1' if product.endswith('_soc') else 'default-aidl'),result.stdout)
    self.assertEqual('r1-health-initial-ready' in result.stdout,product.endswith('_soc'))
    other='lineage_r1' if product.endswith('_soc') else 'lineage_r1_soc'
    mismatch=subprocess.run(['make','--no-print-directory','-f',str(make),'TARGET_PRODUCT='+other],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    self.assertNotEqual(mismatch.returncode,0);self.assertIn('Reinstall r1',mismatch.stdout)
 def test_installer_snapshot_and_fail_before_any_write(self):
  installer=load('health_installer',CANDIDATE/'scripts/install-lineage-device.py')
  original_loader=installer.load_script
  with tempfile.TemporaryDirectory(dir=Path('/rabbitr1/.tmp'),prefix='installer-') as temp:
   root=Path(temp);tree=root/'android';(tree/'.repo').mkdir(parents=True);(tree/'.repo/manifest.xml').write_text('<manifest/>');(tree/'build').mkdir();(tree/'build/envsetup.sh').write_text('');(root/'.tmp').mkdir()
   dist=root/'dist/mainline';dist.mkdir(parents=True);record,files=kernel(OFF)
   files['mt6765-rabbit-r1.dtb']=Path('/rabbitr1/dist/mainline/mt6765-rabbit-r1.dtb').read_bytes()
   record['artifacts']['mt6765-rabbit-r1.dtb']={'bytes':len(files['mt6765-rabbit-r1.dtb']),'sha256':sha(files['mt6765-rabbit-r1.dtb'])}
   for name,data in files.items():(dist/name).write_bytes(data)
   original_record=json.dumps(record).encode();(dist/'build.json').write_bytes(original_record)
   firmware=root/'firmware/stock-v0.8.293';firmware.mkdir(parents=True);(firmware/'dtbo.img').write_bytes(b'pinned fixture DTBO')
   pkg=root/'dist/mtkclient';pkg.mkdir();(pkg/'manifest.json').write_text(json.dumps({'stock_dtbo_sha256':sha(b'pinned fixture DTBO')}))
   installer.ROOT=root;installer.SOURCE=CANDIDATE
   def loader(name):
    return load('fixture_'+name,(CANDIDATE if name=='r1-health-mode' else CANON)/'scripts'/(name+'.py'))
   installer.load_script=loader
   with patch.object(sys,'argv',['install','--tree',str(tree),'--product','lineage_r1_soc']):
    with self.assertRaisesRegex(SystemExit,'Product/kernel SOC mismatch'):installer.main()
   self.assertFalse((tree/'device').exists());self.assertFalse((tree/'.r1-install.json').exists())
   real_mode=loader('r1-health-mode');real_validate=real_mode.validate
   def mutate_after_attestation(*args):
    result=real_validate(*args)
    for name in files:(dist/name).write_bytes(b'changed after attestation')
    (dist/'build.json').write_bytes(b'changed record')
    return result
   real_mode.validate=mutate_after_attestation
   installer.load_script=lambda name:real_mode if name=='r1-health-mode' else loader(name)
   with patch.object(sys,'argv',['install','--tree',str(tree),'--product','lineage_r1']),contextlib.redirect_stdout(io.StringIO()):installer.main()
   prebuilt=tree/'device/rabbit/r1/prebuilt'
   self.assertEqual((prebuilt/'Image.gz').read_bytes(),files['Image.gz'])
   self.assertEqual((prebuilt/'kernel-config').read_bytes(),files['config'])
   self.assertEqual((prebuilt/'kernel-build.json').read_bytes(),original_record)
   self.assertTrue(json.loads((prebuilt/'health-mode.json').read_text())['embedded_config_verified'])
   self.assertFalse(json.loads((tree/'.r1-install.json').read_text())['health_mode']['experimental_soc_health'])
if __name__=='__main__':unittest.main(verbosity=2)
