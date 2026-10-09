/* SPDX-License-Identifier: GPL-2.0-only */

#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;typedef uint16_t u16;typedef uint32_t u32;
#define BIT(x) (1U<<(x))
#define GENMASK(h,l) ((~0U<<(l))&(~0U>>(31-(h))))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#include "mt6765-pm-domains.h"
struct device_node {int kind;u32 id;bool has_id,available;struct device_node *child,*next;};
struct device {struct device_node *of_node;};
struct regmap {int kind;};
struct scpsys {struct device *dev;unsigned int num_bus_prot;const struct scpsys_soc_data *soc_data;u8 bus_prot_index[BUS_PROT_BLOCK_COUNT];struct regmap **bus_prot;};
struct scpsys_domain {struct scpsys *scpsys;};
static struct device_node root,infra_node={1},smi_node={2};
static struct regmap infra={1},smi={2};
static bool have_infra,have_smi,null_handle,modern;
static int explicit_count,map_error;static size_t allocated_count;
#define GFP_KERNEL 0
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define IS_ERR_OR_NULL(p) (!(p)||IS_ERR(p))
#define PTR_ERR(p) ((int)(intptr_t)(p))
static void of_node_get(struct device_node *n){(void)n;}
static void of_node_put(struct device_node *n){(void)n;}
/* Model exact lookup results from old r1: later mediatek,smi exists, no INFRA. */
static struct device_node *of_find_node_with_property(struct device_node *n,const char *p){
 (void)n;if(!strcmp(p,"mediatek,infracfg"))return have_infra?&infra_node:NULL;
 if(!strcmp(p,"mediatek,smi"))return have_smi?&smi_node:NULL;return NULL;
}
static struct regmap *syscon_regmap_lookup_by_phandle(struct device_node *n,const char *p){(void)n;(void)p;return &infra;}
static struct device_node *of_parse_phandle(struct device_node *n,const char *p,int i){
 (void)n;(void)i;if(null_handle)return NULL;return !strcmp(p,"mediatek,smi")?&smi_node:&infra_node;
}
static struct regmap *device_node_to_regmap(struct device_node *n){if(map_error)return(void*)(intptr_t)map_error;return n==&smi_node?&smi:&infra;}
static int of_count_phandle_with_args(struct device_node *n,const char *p,void *a){(void)n;(void)p;(void)a;return explicit_count;}
static void *devm_kmalloc_array(struct device *d,size_t n,size_t size,int g){(void)d;(void)g;allocated_count=n;return calloc(n?n:1,size);}
static int dev_err_probe(struct device *d,int error,const char *fmt,...){(void)d;(void)fmt;return error;}

#define U8_MAX UINT8_MAX
static struct device_node *next_child(struct device_node *p,struct device_node *old,bool available){
 struct device_node *n=old?old->next:p->child;while(n && available && !n->available)n=n->next;return n;
}
#define for_each_child_of_node_scoped(p,n) for(struct device_node *n=next_child(p,NULL,false);n;n=next_child(p,n,false))
#define for_each_available_child_of_node_scoped(p,n) for(struct device_node *n=next_child(p,NULL,true);n;n=next_child(p,n,true))
static int of_property_read_u32(struct device_node *n,const char *p,u32 *id){assert(!strcmp(p,"reg"));if(!n->has_id)return -EINVAL;*id=n->id;return 0;}
static void *of_find_property(struct device_node *n,const char *p,void *length){(void)n;(void)length;assert(!strcmp(p,"access-controllers"));return modern?&root:NULL;}
static unsigned admitted;


/* @PROVIDER_HELPERS@ */
/* @PROBE_ADMISSION@ */

static struct device dev={&root};
static struct device_node mfg,plain;
static void setup(struct scpsys *sc){
 memset(sc,0,sizeof(*sc));sc->soc_data=&mt6765_scpsys_data;sc->dev=&dev;
 have_infra=false;have_smi=false;null_handle=false;explicit_count=1;map_error=0;allocated_count=0;modern=true;admitted=0;
 mfg=(struct device_node){.id=MT6765_POWER_DOMAIN_MFG,.has_id=true,.available=true};
 plain=(struct device_node){.id=MT6765_POWER_DOMAIN_DISP,.has_id=true,.available=true};
 root.child=&mfg;
}
static void finish(struct scpsys *sc,int ret,int want){assert(ret==want);assert(admitted==(want==0));free(sc->bus_prot);sc->bus_prot=NULL;}
static const struct scpsys_domain_data need_status={.sta_mask=1,.bp_cfg={{.bus_prot_block=BUS_PROT_BLOCK_INFRA,.bus_prot_sta_block=BUS_PROT_BLOCK_SMI,.bus_prot_set_clr_mask=1}}};
static const struct scpsys_domain_data bad_kind={.sta_mask=1,.bp_cfg={{.bus_prot_block=255,.bus_prot_sta_block=BUS_PROT_BLOCK_INFRA,.bus_prot_set_clr_mask=1}}};
static const struct scpsys_domain_data bad_status={.sta_mask=1,.bp_cfg={{.bus_prot_block=BUS_PROT_BLOCK_INFRA,.bus_prot_sta_block=255,.bus_prot_set_clr_mask=1}}};
static const struct scpsys_domain_data hole={.sta_mask=1,.bp_cfg={[3]={.bus_prot_block=BUS_PROT_BLOCK_SMI,.bus_prot_sta_block=BUS_PROT_BLOCK_SMI,.bus_prot_set_clr_mask=1}}};
static const struct scpsys_domain_data undefined={0};
int main(void){
 struct scpsys sc;struct scpsys_domain pd={&sc};struct scpsys_soc_data soc;int ret;unsigned controls=0;
 const struct scpsys_bus_prot_data *bp=&scpsys_domain_data_mt6765[MT6765_POWER_DOMAIN_MFG].bp_cfg[0];
 setup(&sc);ret=pipeline(&dev,&sc);assert(sc.num_bus_prot==1 && sc.bus_prot_index[BUS_PROT_BLOCK_INFRA]==0 && scpsys_bus_protect_get_regmap(&pd,bp)==&infra);finish(&sc,ret,0);controls++;
 for(int n=0;n<=2;n+=2){setup(&sc);explicit_count=n;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;}
 setup(&sc);null_handle=true;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;
 setup(&sc);map_error=-517;finish(&sc,pipeline(&dev,&sc),-517);controls++;
 setup(&sc);modern=false;have_infra=have_smi=true;ret=pipeline(&dev,&sc);assert(sc.num_bus_prot==2 && scpsys_bus_protect_get_regmap(&pd,bp)==&infra);finish(&sc,ret,0);controls++;
 setup(&sc);modern=false;have_infra=true;ret=pipeline(&dev,&sc);assert(sc.num_bus_prot==1 && sc.bus_prot_index[BUS_PROT_BLOCK_INFRA]==0);finish(&sc,ret,0);controls++;
 setup(&sc);modern=false;have_smi=true;ret=pipeline(&dev,&sc);assert(sc.num_bus_prot==1 && sc.bus_prot_index[BUS_PROT_BLOCK_INFRA]==U8_MAX && sc.bus_prot_index[BUS_PROT_BLOCK_SMI]==0);finish(&sc,ret,-ENODEV);controls++;
 setup(&sc);modern=false;ret=pipeline(&dev,&sc);assert(sc.num_bus_prot==0);finish(&sc,ret,-ENODEV);controls++;
 /* The omitted/disabled top-level MFG must not create a requirement. */
 setup(&sc);modern=false;have_smi=true;root.child=&plain;finish(&sc,pipeline(&dev,&sc),0);controls++;
 setup(&sc);modern=false;mfg.available=false;mfg.next=&plain;finish(&sc,pipeline(&dev,&sc),0);controls++;
 /* Existing nested traversal visits disabled nodes too. */
 setup(&sc);modern=false;root.child=&plain;plain.child=&mfg;mfg.available=false;finish(&sc,pipeline(&dev,&sc),-ENODEV);controls++;
 setup(&sc);mfg.id=UINT32_MAX;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;
 setup(&sc);mfg.has_id=false;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;
 const struct scpsys_domain_data *data[]={&need_status,&bad_kind,&bad_status,&hole,&undefined};
 int wanted[]={-ENODEV,-EINVAL,-EINVAL,-ENODEV,-EINVAL};
 for(unsigned i=0;i<ARRAY_SIZE(data);i++){setup(&sc);soc=mt6765_scpsys_data;soc.domains_data=data[i];soc.num_domains=1;sc.soc_data=&soc;mfg.id=0;finish(&sc,pipeline(&dev,&sc),wanted[i]);controls++;}
 /* The required status bank is allowed when actually discovered. */
 setup(&sc);modern=false;have_infra=have_smi=true;soc=mt6765_scpsys_data;soc.domains_data=&need_status;soc.num_domains=1;sc.soc_data=&soc;mfg.id=0;finish(&sc,pipeline(&dev,&sc),0);controls++;
 /* Invalid and duplicate modern kinds never index out-of-range. */
 enum scpsys_bus_prot_block kinds[]={BUS_PROT_BLOCK_COUNT,BUS_PROT_BLOCK_INFRA};
 setup(&sc);soc=mt6765_scpsys_data;soc.bus_prot_blocks=kinds;sc.soc_data=&soc;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;
 kinds[0]=BUS_PROT_BLOCK_INFRA;setup(&sc);soc=mt6765_scpsys_data;soc.bus_prot_blocks=kinds;soc.num_bus_prot_blocks=2;explicit_count=2;sc.soc_data=&soc;finish(&sc,pipeline(&dev,&sc),-EINVAL);controls++;
 /* HWV requires its own ID bounds, no direct table or protection maps. */
 for(unsigned id=0;id<=1;id++){setup(&sc);modern=false;soc=(struct scpsys_soc_data){.type=SCPSYS_MTCMOS_TYPE_HW_VOTER,.num_hwv_domains=1};sc.soc_data=&soc;mfg.id=id;finish(&sc,pipeline(&dev,&sc),id?-EINVAL:0);controls++;}
 setup(&sc);assert(pipeline(&dev,&sc)==0);sc.bus_prot_index[BUS_PROT_BLOCK_INFRA]=sc.num_bus_prot;assert(scpsys_validate_bus_protection_domain(&sc,&mfg)==-ENODEV);free(sc.bus_prot);controls++;
 setup(&sc);assert(pipeline(&dev,&sc)==0);sc.bus_prot[0]=NULL;assert(scpsys_validate_bus_protection_domain(&sc,&mfg)==-ENODEV);free(sc.bus_prot);controls++;
 printf("{\"passed\":true,\"controls\":%u,\"smi_only_and_absent_maps_rejected_before_admission\":true,\"legacy_index_zero_and_omitted_domains_preserved\":true}\n",controls);return 0;
}
