// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2015 MediaTek Inc.
 */

#include <drm/drm_blend.h>
#include <drm/drm_fourcc.h>
#include <drm/drm_framebuffer.h>
#include <drm/drm_gem_dma_helper.h>

#include <linux/align.h>
#include <linux/clk.h>
#include <linux/component.h>
#include <linux/delay.h>
#include <linux/interrupt.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/soc/mediatek/mtk-cmdq.h>

#include "mtk_crtc.h"
#include "mtk_ddp_comp.h"
#include "mtk_disp_drv.h"
#include "mtk_drm_drv.h"

#define DISP_REG_OVL_INTEN			0x0004
#define OVL_FME_CPL_INT					BIT(1)
#define DISP_REG_OVL_INTSTA			0x0008
#define DISP_REG_OVL_EN				0x000c
#define DISP_REG_OVL_RST			0x0014
#define DISP_REG_OVL_ROI_SIZE			0x0020
#define DISP_REG_OVL_DATAPATH_CON		0x0024
#define OVL_LAYER_SMI_ID_EN				BIT(0)
#define OVL_BGCLR_SEL_IN				BIT(2)
#define OVL_LAYER_AFBC_EN(n)				BIT(4+n)
#define DISP_REG_OVL_ROI_BGCLR			0x0028
#define DISP_REG_OVL_SRC_CON			0x002c
#define DISP_REG_OVL_CON(n)			(0x0030 + 0x20 * (n))
#define DISP_REG_OVL_SRCKEY(n)			(0x0034 + 0x20 * (n))
#define DISP_REG_OVL_SRC_SIZE(n)		(0x0038 + 0x20 * (n))
#define DISP_REG_OVL_OFFSET(n)			(0x003c + 0x20 * (n))
#define DISP_REG_OVL_PITCH_MSB(n)		(0x0040 + 0x20 * (n))
#define OVL_PITCH_MSB_2ND_SUBBUF			BIT(16)
#define DISP_REG_OVL_PITCH(n)			(0x0044 + 0x20 * (n))
#define OVL_CONST_BLEND					BIT(28)
#define DISP_REG_OVL_CLIP(n)			(0x004c + 0x20 * (n))
#define OVL_CLIP_LEFT				BIT(0)
#define OVL_CLIP_RIGHT				BIT(8)
#define DISP_REG_OVL_CLR(n)			(0x025c + 4 * (n))
#define DISP_REG_OVL_SECURE			0x0fc0
#define DISP_REG_OVL_RDMA_CTRL(n)		(0x00c0 + 0x20 * (n))
#define DISP_REG_OVL_RDMA_GMC(n)		(0x00c8 + 0x20 * (n))
#define DISP_REG_OVL_ADDR_MT2701		0x0040
#define DISP_REG_OVL_CLRFMT_EXT			0x02d0
#define OVL_CON_CLRFMT_BIT_DEPTH_MASK(n)		(GENMASK(1, 0) << (4 * (n)))
#define OVL_CON_CLRFMT_BIT_DEPTH(depth, n)		((depth) << (4 * (n)))
#define OVL_CON_CLRFMT_8_BIT				(0)
#define OVL_CON_CLRFMT_10_BIT				(1)
#define DISP_REG_OVL_ADDR_MT8173		0x0f40
#define DISP_REG_OVL_ADDR(ovl, n)		((ovl)->data->addr + 0x20 * (n))
#define DISP_REG_OVL_HDR_ADDR(ovl, n)		((ovl)->data->addr + 0x20 * (n) + 0x04)
#define DISP_REG_OVL_HDR_PITCH(ovl, n)		((ovl)->data->addr + 0x20 * (n) + 0x08)

#define GMC_THRESHOLD_BITS	16
#define GMC_THRESHOLD_HIGH	((1 << GMC_THRESHOLD_BITS) / 4)
#define GMC_THRESHOLD_LOW	((1 << GMC_THRESHOLD_BITS) / 8)

#define OVL_CON_CLRFMT_MAN	BIT(23)
#define OVL_CON_BYTE_SWAP	BIT(24)

/* OVL_CON_RGB_SWAP works only if OVL_CON_CLRFMT_MAN is enabled */
#define OVL_CON_RGB_SWAP	BIT(25)

#define OVL_CON_CLRFMT_RGB	(1 << 12)
#define OVL_CON_CLRFMT_ARGB8888	(2 << 12)
#define OVL_CON_CLRFMT_RGBA8888	(3 << 12)
#define OVL_CON_CLRFMT_ABGR8888	(OVL_CON_CLRFMT_ARGB8888 | OVL_CON_BYTE_SWAP)
#define OVL_CON_CLRFMT_BGRA8888	(OVL_CON_CLRFMT_RGBA8888 | OVL_CON_BYTE_SWAP)
#define OVL_CON_CLRFMT_UYVY	(4 << 12)
#define OVL_CON_CLRFMT_YUYV	(5 << 12)
#define OVL_CON_MTX_YUV_TO_RGB	(6 << 16)
#define OVL_CON_CLRFMT_PARGB8888 ((3 << 12) | OVL_CON_CLRFMT_MAN)
#define OVL_CON_CLRFMT_PABGR8888 (OVL_CON_CLRFMT_PARGB8888 | OVL_CON_RGB_SWAP)
#define OVL_CON_CLRFMT_PBGRA8888 (OVL_CON_CLRFMT_PARGB8888 | OVL_CON_BYTE_SWAP)
#define OVL_CON_CLRFMT_PRGBA8888 (OVL_CON_CLRFMT_PABGR8888 | OVL_CON_BYTE_SWAP)
#define OVL_CON_CLRFMT_RGB565(ovl)	((ovl)->data->fmt_rgb565_is_0 ? \
					0 : OVL_CON_CLRFMT_RGB)
#define OVL_CON_CLRFMT_RGB888(ovl)	((ovl)->data->fmt_rgb565_is_0 ? \
					OVL_CON_CLRFMT_RGB : 0)
#define	OVL_CON_AEN		BIT(8)
#define	OVL_CON_ALPHA		0xff
#define	OVL_CON_VIRT_FLIP	BIT(9)
#define	OVL_CON_HORZ_FLIP	BIT(10)

#define OVL_COLOR_ALPHA		GENMASK(31, 24)

#define OVL_ENGINE_EN		BIT(0)
#define OVL_HF_CLK_EN		BIT(10)
#define OVL_BLOCK_EXT_ULTRA	BIT(18)
#define OVL_BLOCK_EXT_PREULTRA	BIT(19)
#define OVL_RANDOM_BGCLR_EN	BIT(1)
#define OVL_OUTPUT_NO_RND		BIT(3)
#define OVL_GCLAST_EN		BIT(24)
#define OVL_OUTPUT_CLAMP		BIT(26)
#define OVL_FRAME_UNDERFLOW	BIT(2)
#define OVL_ABNORMAL_SOF		BIT(13)
#define OVL_ABNORMAL_EOF(layers)	GENMASK((layers) + 4, 5)
#define DISP_REG_OVL_FIFO_CTRL(n)	(0x00d0 + 0x20 * (n))
#define DISP_REG_OVL_GMC_S2(n)	(0x01e0 + 4 * (n))
#define DISP_REG_OVL_GREQ_NUM	0x01f8
#define DISP_REG_OVL_GREQ_URG	0x01fc
#define DISP_REG_OVL_ULTRA_SRC	0x020c
#define DISP_REG_OVL_BUF_LOW(n)	(0x0210 + 4 * (n))
#define DISP_REG_OVL_BUF_HIGH(n)	(0x0220 + 4 * (n))
#define DISP_REG_OVL_FLOW_CTRL_DBG 0x0240
#define DISP_REG_OVL_LC_SRC_SIZE	0x0288
#define DISP_REG_OVL_FUNC_DCM0	0x02a0
#define DISP_REG_OVL_FUNC_DCM1	0x02a4
#define DISP_REG_OVL_EXT_CON	0x0324
#define DISP_REG_OVL_SBCH		0x03a0
#define DISP_REG_OVL_SBCH_EXT	0x03a4
#define MT6765_OVL_GMC		0x03ff03ff

static inline bool is_10bit_rgb(u32 fmt)
{
	switch (fmt) {
	case DRM_FORMAT_XRGB2101010:
	case DRM_FORMAT_ARGB2101010:
	case DRM_FORMAT_RGBX1010102:
	case DRM_FORMAT_RGBA1010102:
	case DRM_FORMAT_XBGR2101010:
	case DRM_FORMAT_ABGR2101010:
	case DRM_FORMAT_BGRX1010102:
	case DRM_FORMAT_BGRA1010102:
		return true;
	}
	return false;
}

static const u32 mt8173_formats[] = {
	DRM_FORMAT_XRGB8888,
	DRM_FORMAT_ARGB8888,
	DRM_FORMAT_BGRX8888,
	DRM_FORMAT_BGRA8888,
	DRM_FORMAT_ABGR8888,
	DRM_FORMAT_XBGR8888,
	DRM_FORMAT_RGB888,
	DRM_FORMAT_BGR888,
	DRM_FORMAT_RGB565,
	DRM_FORMAT_UYVY,
	DRM_FORMAT_YUYV,
};

static const u32 mt8195_formats[] = {
	DRM_FORMAT_XRGB8888,
	DRM_FORMAT_ARGB8888,
	DRM_FORMAT_XRGB2101010,
	DRM_FORMAT_ARGB2101010,
	DRM_FORMAT_BGRX8888,
	DRM_FORMAT_BGRA8888,
	DRM_FORMAT_BGRX1010102,
	DRM_FORMAT_BGRA1010102,
	DRM_FORMAT_ABGR8888,
	DRM_FORMAT_XBGR8888,
	DRM_FORMAT_XBGR2101010,
	DRM_FORMAT_ABGR2101010,
	DRM_FORMAT_RGBX8888,
	DRM_FORMAT_RGBA8888,
	DRM_FORMAT_RGBX1010102,
	DRM_FORMAT_RGBA1010102,
	DRM_FORMAT_RGB888,
	DRM_FORMAT_BGR888,
	DRM_FORMAT_RGB565,
	DRM_FORMAT_UYVY,
	DRM_FORMAT_YUYV,
};

struct mtk_disp_ovl_data {
	unsigned int addr;
	unsigned int gmc_bits;
	unsigned int layer_nr;
	bool fmt_rgb565_is_0;
	bool smi_id_en;
	bool supports_afbc;
	const u32 blend_modes;
	const u32 *formats;
	size_t num_formats;
	bool supports_clrfmt_ext;
	bool mt6765;
};

/*
 * struct mtk_disp_ovl - DISP_OVL driver structure
 * @crtc: associated crtc to report vblank events to
 * @data: platform data
 */
struct mtk_disp_ovl {
	struct device			*dev;
	struct drm_crtc			*crtc;
	struct clk			*clk;
	void __iomem			*regs;
	struct cmdq_client_reg		cmdq_reg;
	const struct mtk_disp_ovl_data	*data;
	void				(*vblank_cb)(void *data);
	void				*vblank_cb_data;
	int				irq;
	bool				clock_enabled, config_valid;
	u32				error_count;
};

static u32 mt6765_ovl_error_mask(struct mtk_disp_ovl *ovl)
{
	return OVL_FRAME_UNDERFLOW | OVL_ABNORMAL_SOF |
	       OVL_ABNORMAL_EOF(ovl->data->layer_nr);
}

static irqreturn_t mt6765_ovl_irq(struct mtk_disp_ovl *ovl)
{
	u32 status = readl(ovl->regs + DISP_REG_OVL_INTSTA);
	u32 enabled;

	if (!status)
		return IRQ_NONE;

	enabled = readl(ovl->regs + DISP_REG_OVL_INTEN);
	writel(~status, ovl->regs + DISP_REG_OVL_INTSTA);
	if (status & mt6765_ovl_error_mask(ovl)) {
		ovl->error_count++;
		dev_err_ratelimited(ovl->dev, "OVL error: count=%u status=%#x flow=%#x\n",
				    ovl->error_count, status,
				    readl(ovl->regs + DISP_REG_OVL_FLOW_CTRL_DBG));
	}
	if ((status & enabled & OVL_FME_CPL_INT) && ovl->vblank_cb)
		ovl->vblank_cb(ovl->vblank_cb_data);

	return IRQ_HANDLED;
}

static irqreturn_t mtk_disp_ovl_irq_handler(int irq, void *dev_id)
{
	struct mtk_disp_ovl *priv = dev_id;

	if (priv->data->mt6765)
		return mt6765_ovl_irq(priv);

	/* Clear frame completion interrupt */
	writel(0x0, priv->regs + DISP_REG_OVL_INTSTA);

	if (!priv->vblank_cb)
		return IRQ_NONE;

	priv->vblank_cb(priv->vblank_cb_data);

	return IRQ_HANDLED;
}

void mtk_ovl_register_vblank_cb(struct device *dev,
				void (*vblank_cb)(void *),
				void *vblank_cb_data)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	ovl->vblank_cb = vblank_cb;
	ovl->vblank_cb_data = vblank_cb_data;
}

void mtk_ovl_unregister_vblank_cb(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	ovl->vblank_cb = NULL;
	ovl->vblank_cb_data = NULL;
}

void mtk_ovl_enable_vblank(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		writel(~(u32)OVL_FME_CPL_INT, ovl->regs + DISP_REG_OVL_INTSTA);
		writel(readl(ovl->regs + DISP_REG_OVL_INTEN) | OVL_FME_CPL_INT,
		       ovl->regs + DISP_REG_OVL_INTEN);
		return;
	}
	writel(0x0, ovl->regs + DISP_REG_OVL_INTSTA);
	writel_relaxed(OVL_FME_CPL_INT, ovl->regs + DISP_REG_OVL_INTEN);
}

void mtk_ovl_disable_vblank(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		writel(readl(ovl->regs + DISP_REG_OVL_INTEN) & ~OVL_FME_CPL_INT,
		       ovl->regs + DISP_REG_OVL_INTEN);
		return;
	}
	writel_relaxed(0x0, ovl->regs + DISP_REG_OVL_INTEN);
}

u32 mtk_ovl_get_blend_modes(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	return ovl->data->blend_modes;
}

const u32 *mtk_ovl_get_formats(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	return ovl->data->formats;
}

size_t mtk_ovl_get_num_formats(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	return ovl->data->num_formats;
}

bool mtk_ovl_is_afbc_supported(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	return ovl->data->supports_afbc;
}

static int mt6765_ovl_reset(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	u32 value;

	writel(1, ovl->regs + DISP_REG_OVL_RST);
	writel(0, ovl->regs + DISP_REG_OVL_RST);
	/* Unlike the stock loop, re-read the state on every poll. */
	return readl_poll_timeout(ovl->regs + DISP_REG_OVL_FLOW_CTRL_DBG, value,
				  value & 3, 10, 20000);
}

int mtk_ovl_clk_enable(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	unsigned int i;
	int ret;

	ret = clk_prepare_enable(ovl->clk);
	if (ret || !ovl->data->mt6765)
		return ret;

	ovl->config_valid = false;
	writel(0, ovl->regs + DISP_REG_OVL_INTEN);
	writel(readl(ovl->regs + DISP_REG_OVL_EN) & ~OVL_ENGINE_EN,
	       ovl->regs + DISP_REG_OVL_EN);
	ret = mt6765_ovl_reset(dev);
	if (ret) {
		dev_err(dev, "OVL reset failed: %d\n", ret);
		clk_disable_unprepare(ovl->clk);
		return ret;
	}

	/* Drop inherited physical, constant-color and extended layers. */
	writel(0, ovl->regs + DISP_REG_OVL_SRC_CON);
	writel(0, ovl->regs + DISP_REG_OVL_EXT_CON);
	/* Mainline does not track SBCH's reusable transparent/constant regions. */
	writel(0, ovl->regs + DISP_REG_OVL_SBCH);
	writel(0, ovl->regs + DISP_REG_OVL_SBCH_EXT);
	/* CRTC will enable the upstream background input on the second overlay. */
	writel(readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON) &
	       ~(OVL_BGCLR_SEL_IN | OVL_RANDOM_BGCLR_EN),
	       ovl->regs + DISP_REG_OVL_DATAPATH_CON);
	for (i = 0; i < ovl->data->layer_nr; i++)
		writel(0, ovl->regs + DISP_REG_OVL_RDMA_CTRL(i));
	writel(0, ovl->regs + DISP_REG_OVL_INTSTA);
	ovl->clock_enabled = true;
	enable_irq(ovl->irq);
	return 0;
}

void mtk_ovl_clk_disable(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		int ret;

		ovl->config_valid = false;
		writel(readl(ovl->regs + DISP_REG_OVL_EN) & ~OVL_ENGINE_EN,
		       ovl->regs + DISP_REG_OVL_EN);
		writel(0, ovl->regs + DISP_REG_OVL_INTEN);
		disable_irq(ovl->irq);
		writel(0, ovl->regs + DISP_REG_OVL_INTSTA);
		/*
		 * Returning lets CRTC teardown release DMA buffers. Keep the clock
		 * and wait for reset completion, even after a persistent failure.
		 */
		while ((ret = mt6765_ovl_reset(dev))) {
			dev_err_ratelimited(dev, "OVL teardown reset failed: %d\n", ret);
			msleep(20);
		}
		ovl->clock_enabled = false;
	}
	clk_disable_unprepare(ovl->clk);
}

void mtk_ovl_start(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		u32 reg;

		if (!ovl->config_valid)
			return;
		reg = readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON);
		reg &= ~OVL_OUTPUT_NO_RND;
		reg |= OVL_LAYER_SMI_ID_EN | OVL_GCLAST_EN | OVL_OUTPUT_CLAMP;
		writel(reg, ovl->regs + DISP_REG_OVL_DATAPATH_CON);
		writel(readl(ovl->regs + DISP_REG_OVL_INTEN) | mt6765_ovl_error_mask(ovl),
		       ovl->regs + DISP_REG_OVL_INTEN);
		writel(readl(ovl->regs + DISP_REG_OVL_EN) | OVL_ENGINE_EN | OVL_HF_CLK_EN,
		       ovl->regs + DISP_REG_OVL_EN);
		return;
	}
	if (ovl->data->smi_id_en) {
		unsigned int reg;

		reg = readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON);
		reg = reg | OVL_LAYER_SMI_ID_EN;
		writel_relaxed(reg, ovl->regs + DISP_REG_OVL_DATAPATH_CON);
	}
	writel_relaxed(0x1, ovl->regs + DISP_REG_OVL_EN);
}

void mtk_ovl_stop(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		writel(0, ovl->regs + DISP_REG_OVL_INTEN);
		writel(readl(ovl->regs + DISP_REG_OVL_EN) & ~OVL_ENGINE_EN,
		       ovl->regs + DISP_REG_OVL_EN);
		writel(0, ovl->regs + DISP_REG_OVL_INTSTA);
		ovl->config_valid = false;
		return;
	}
	writel_relaxed(0x0, ovl->regs + DISP_REG_OVL_EN);
	if (ovl->data->smi_id_en) {
		unsigned int reg;

		reg = readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON);
		reg = reg & ~OVL_LAYER_SMI_ID_EN;
		writel_relaxed(reg, ovl->regs + DISP_REG_OVL_DATAPATH_CON);
	}
}

static void mtk_ovl_set_afbc(struct mtk_disp_ovl *ovl, struct cmdq_pkt *cmdq_pkt,
			     int idx, bool enabled)
{
	mtk_ddp_write_mask(cmdq_pkt, enabled ? OVL_LAYER_AFBC_EN(idx) : 0,
			   &ovl->cmdq_reg, ovl->regs,
			   DISP_REG_OVL_DATAPATH_CON, OVL_LAYER_AFBC_EN(idx));
}

static void mtk_ovl_set_bit_depth(struct device *dev, int idx, u32 format,
				  struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	unsigned int bit_depth = OVL_CON_CLRFMT_8_BIT;

	if (!ovl->data->supports_clrfmt_ext)
		return;

	if (is_10bit_rgb(format))
		bit_depth = OVL_CON_CLRFMT_10_BIT;

	mtk_ddp_write_mask(cmdq_pkt, OVL_CON_CLRFMT_BIT_DEPTH(bit_depth, idx),
			   &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_CLRFMT_EXT,
			   OVL_CON_CLRFMT_BIT_DEPTH_MASK(idx));
}

static void mt6765_ovl_config(struct mtk_disp_ovl *ovl, unsigned int w,
			      unsigned int h, struct cmdq_pkt *pkt)
{
	u32 greq = ovl->data->layer_nr == 4 ? 0x7777 : 0x77;
	unsigned int i;

	if (!ovl->clock_enabled || !w || !h || w > 4095 || h > 4095) {
		ovl->config_valid = false;
		if (ovl->clock_enabled)
			mtk_ddp_write_mask(pkt, 0, &ovl->cmdq_reg, ovl->regs,
					   DISP_REG_OVL_EN, OVL_ENGINE_EN);
		dev_err_ratelimited(ovl->dev, "Unsupported OVL mode %ux%u\n", w, h);
		return;
	}

	mtk_ddp_write(pkt, h << 16 | w, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_ROI_SIZE);
	mtk_ddp_write(pkt, OVL_COLOR_ALPHA, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_ROI_BGCLR);
	mtk_ddp_write_mask(pkt, h << 16 | w, &ovl->cmdq_reg, ovl->regs,
			   DISP_REG_OVL_LC_SRC_SIZE, GENMASK(28, 16) | GENMASK(12, 0));

	/* Stock real-time FIFO/request policy; each FIFO has 192 words. */
	for (i = 0; i < ovl->data->layer_nr; i++) {
		mtk_ddp_write(pkt, MT6765_OVL_GMC, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_RDMA_GMC(i));
		mtk_ddp_write(pkt, 192 << 16, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_FIFO_CTRL(i));
		mtk_ddp_write(pkt, 0x203f007f, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_GMC_S2(i));
		mtk_ddp_write(pkt, 0, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_BUF_LOW(i));
		mtk_ddp_write(pkt, BIT(31), &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_BUF_HIGH(i));
	}
	mtk_ddp_write(pkt, 0xf1ff0000 | greq, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_GREQ_NUM);
	/* The shipped two-layer urgent setting also programs the third field. */
	mtk_ddp_write(pkt, ovl->data->layer_nr == 4 ? 0x7777 : 0x777,
		      &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_GREQ_URG);
	mtk_ddp_write(pkt, 0x8040, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_ULTRA_SRC);
	mtk_ddp_write(pkt, 0, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_FUNC_DCM0);
	mtk_ddp_write(pkt, 0, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_FUNC_DCM1);
	mtk_ddp_write_mask(pkt, 0, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_EN,
			   OVL_BLOCK_EXT_ULTRA | OVL_BLOCK_EXT_PREULTRA);
	ovl->config_valid = true;
}

void mtk_ovl_config(struct device *dev, unsigned int w,
		    unsigned int h, unsigned int vrefresh,
		    unsigned int bpc, struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	if (ovl->data->mt6765) {
		mt6765_ovl_config(ovl, w, h, cmdq_pkt);
		return;
	}
	if (w != 0 && h != 0)
		mtk_ddp_write_relaxed(cmdq_pkt, h << 16 | w, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_ROI_SIZE);

	/*
	 * The background color must be opaque black (ARGB),
	 * otherwise the alpha blending will have no effect
	 */
	mtk_ddp_write_relaxed(cmdq_pkt, OVL_COLOR_ALPHA, &ovl->cmdq_reg,
			      ovl->regs, DISP_REG_OVL_ROI_BGCLR);

	mtk_ddp_write(cmdq_pkt, 0x1, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_RST);
	mtk_ddp_write(cmdq_pkt, 0x0, &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_RST);
}

unsigned int mtk_ovl_layer_nr(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	return ovl->data->layer_nr;
}

unsigned int mtk_ovl_supported_rotations(struct device *dev)
{
	return DRM_MODE_ROTATE_0 | DRM_MODE_ROTATE_180 |
	       DRM_MODE_REFLECT_X | DRM_MODE_REFLECT_Y;
}

static int mt6765_ovl_plane_check(struct mtk_disp_ovl *ovl, unsigned int idx,
				struct drm_plane_state *state)
{
	struct drm_framebuffer *fb = state->fb;
	struct drm_gem_dma_object *obj;
	unsigned int x, y, w, h, cpp = fb->format->cpp[0];
	u64 end;

	if (!state->visible)
		return 0;
	if (idx >= ovl->data->layer_nr || fb->modifier != DRM_FORMAT_MOD_LINEAR ||
	    !cpp || !fb->pitches[0] || fb->pitches[0] > U16_MAX)
		return -EINVAL;
	/* Component checks run after DRM has clipped and unrotated the source. */
	if (state->src.x1 < 0 || state->src.y1 < 0 ||
	    ((state->src.x1 | state->src.y1 | state->src.x2 | state->src.y2) & 0xffff))
		return -EINVAL;
	x = state->src.x1 >> 16;
	y = state->src.y1 >> 16;
	w = drm_rect_width(&state->src) >> 16;
	h = drm_rect_height(&state->src) >> 16;
	if (!w || !h || w > 4095 || h > 4095)
		return -EINVAL;
	if (fb->format->is_yuv) {
		w = ALIGN(w + (x & 1), 2);
		x &= ~1U;
	}
	if (w > 4095 ||
	    state->dst.x1 < 0 || state->dst.y1 < 0 ||
	    state->dst.x2 > 4095 || state->dst.y2 > 4095 ||
	    (u64)(x + w) * cpp > fb->pitches[0])
		return -EINVAL;

	obj = to_drm_gem_dma_obj(fb->obj[0]);
	end = (u64)fb->offsets[0] + (u64)(y + h - 1) * fb->pitches[0] +
	      (u64)(x + w) * cpp;
	if (end > obj->base.size || obj->dma_addr > U32_MAX ||
	    obj->dma_addr + end - 1 > U32_MAX)
		return -EINVAL;

	return 0;
}

int mtk_ovl_layer_check(struct device *dev, unsigned int idx,
			struct mtk_plane_state *mtk_state)
{
	struct drm_plane_state *state = &mtk_state->base;
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	/* check if any unsupported rotation is set */
	if (state->rotation & ~mtk_ovl_supported_rotations(dev))
		return -EINVAL;

	/*
	 * TODO: Rotating/reflecting YUV buffers is not supported at this time.
	 *	 Only RGB[AX] variants are supported.
	 *	 Since DRM_MODE_ROTATE_0 means "no rotation", we should not
	 *	 reject layers with this property.
	 */
	if (state->fb->format->is_yuv && (state->rotation & ~DRM_MODE_ROTATE_0))
		return -EINVAL;

	if (ovl->data->mt6765)
		return mt6765_ovl_plane_check(ovl, idx, state);

	return 0;
}

void mtk_ovl_layer_on(struct device *dev, unsigned int idx,
		      struct cmdq_pkt *cmdq_pkt)
{
	unsigned int gmc_thrshd_l;
	unsigned int gmc_thrshd_h;
	unsigned int gmc_value;
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	mtk_ddp_write(cmdq_pkt, 0x1, &ovl->cmdq_reg, ovl->regs,
		      DISP_REG_OVL_RDMA_CTRL(idx));
	gmc_thrshd_l = GMC_THRESHOLD_LOW >>
		      (GMC_THRESHOLD_BITS - ovl->data->gmc_bits);
	gmc_thrshd_h = GMC_THRESHOLD_HIGH >>
		      (GMC_THRESHOLD_BITS - ovl->data->gmc_bits);
	if (ovl->data->mt6765)
		gmc_value = MT6765_OVL_GMC;
	else if (ovl->data->gmc_bits == 10)
		gmc_value = gmc_thrshd_h | gmc_thrshd_h << 16;
	else
		gmc_value = gmc_thrshd_l | gmc_thrshd_l << 8 |
			    gmc_thrshd_h << 16 | gmc_thrshd_h << 24;
	mtk_ddp_write(cmdq_pkt, gmc_value,
		      &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_RDMA_GMC(idx));
	mtk_ddp_write_mask(cmdq_pkt, BIT(idx), &ovl->cmdq_reg, ovl->regs,
			   DISP_REG_OVL_SRC_CON, BIT(idx));
}

void mtk_ovl_layer_off(struct device *dev, unsigned int idx,
		       struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);

	mtk_ddp_write_mask(cmdq_pkt, 0, &ovl->cmdq_reg, ovl->regs,
			   DISP_REG_OVL_SRC_CON, BIT(idx));
	mtk_ddp_write(cmdq_pkt, 0, &ovl->cmdq_reg, ovl->regs,
		      DISP_REG_OVL_RDMA_CTRL(idx));
}

static unsigned int mtk_ovl_fmt_convert(struct mtk_disp_ovl *ovl,
					struct mtk_plane_state *state)
{
	unsigned int fmt = state->pending.format;
	unsigned int blend_mode = DRM_MODE_BLEND_COVERAGE;

	/*
	 * For the platforms where OVL_CON_CLRFMT_MAN is defined in the hardware data sheet
	 * and supports premultiplied color formats, such as OVL_CON_CLRFMT_PARGB8888.
	 *
	 * Check blend_modes in the driver data to see if premultiplied mode is supported.
	 * If not, use coverage mode instead to set it to the supported color formats.
	 *
	 * Current DRM assumption is that alpha is default premultiplied, so the bitmask of
	 * blend_modes must include BIT(DRM_MODE_BLEND_PREMULTI). Otherwise, mtk_plane_init()
	 * will get an error return from drm_plane_create_blend_mode_property() and
	 * state->base.pixel_blend_mode should not be used.
	 */
	if (ovl->data->blend_modes & BIT(DRM_MODE_BLEND_PREMULTI))
		blend_mode = state->base.pixel_blend_mode;
	/* No pixel alpha means that the stored RGB values are unassociated. */
	if (ovl->data->mt6765 &&
	    (blend_mode == DRM_MODE_BLEND_PIXEL_NONE ||
	     (state->base.fb && !state->base.fb->format->has_alpha)))
		blend_mode = DRM_MODE_BLEND_COVERAGE;

	switch (fmt) {
	default:
	case DRM_FORMAT_RGB565:
		return OVL_CON_CLRFMT_RGB565(ovl);
	case DRM_FORMAT_BGR565:
		return OVL_CON_CLRFMT_RGB565(ovl) | OVL_CON_BYTE_SWAP;
	case DRM_FORMAT_RGB888:
		return OVL_CON_CLRFMT_RGB888(ovl);
	case DRM_FORMAT_BGR888:
		return OVL_CON_CLRFMT_RGB888(ovl) | OVL_CON_BYTE_SWAP;
	case DRM_FORMAT_RGBX8888:
	case DRM_FORMAT_RGBA8888:
	case DRM_FORMAT_RGBX1010102:
	case DRM_FORMAT_RGBA1010102:
		return blend_mode == DRM_MODE_BLEND_COVERAGE ?
		       OVL_CON_CLRFMT_RGBA8888 :
		       OVL_CON_CLRFMT_PRGBA8888;
	case DRM_FORMAT_BGRX8888:
	case DRM_FORMAT_BGRA8888:
	case DRM_FORMAT_BGRX1010102:
	case DRM_FORMAT_BGRA1010102:
		return blend_mode == DRM_MODE_BLEND_COVERAGE ?
		       OVL_CON_CLRFMT_BGRA8888 :
		       OVL_CON_CLRFMT_PBGRA8888;
	case DRM_FORMAT_XRGB8888:
	case DRM_FORMAT_ARGB8888:
	case DRM_FORMAT_XRGB2101010:
	case DRM_FORMAT_ARGB2101010:
		return blend_mode == DRM_MODE_BLEND_COVERAGE ?
		       OVL_CON_CLRFMT_ARGB8888 :
		       OVL_CON_CLRFMT_PARGB8888;
	case DRM_FORMAT_XBGR8888:
	case DRM_FORMAT_ABGR8888:
	case DRM_FORMAT_XBGR2101010:
	case DRM_FORMAT_ABGR2101010:
		return blend_mode == DRM_MODE_BLEND_COVERAGE ?
		       OVL_CON_CLRFMT_ABGR8888 :
		       OVL_CON_CLRFMT_PABGR8888;
	case DRM_FORMAT_UYVY:
		return OVL_CON_CLRFMT_UYVY | OVL_CON_MTX_YUV_TO_RGB;
	case DRM_FORMAT_YUYV:
		return OVL_CON_CLRFMT_YUYV | OVL_CON_MTX_YUV_TO_RGB;
	}
}

static void mtk_ovl_afbc_layer_config(struct mtk_disp_ovl *ovl,
				      unsigned int idx,
				      struct mtk_plane_pending_state *pending,
				      struct cmdq_pkt *cmdq_pkt)
{
	unsigned int pitch_msb = pending->pitch >> 16;
	unsigned int hdr_pitch = pending->hdr_pitch;
	unsigned int hdr_addr = pending->hdr_addr;

	if (pending->modifier != DRM_FORMAT_MOD_LINEAR) {
		mtk_ddp_write_relaxed(cmdq_pkt, hdr_addr, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_HDR_ADDR(ovl, idx));
		mtk_ddp_write_relaxed(cmdq_pkt,
				      OVL_PITCH_MSB_2ND_SUBBUF | pitch_msb,
				      &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_PITCH_MSB(idx));
		mtk_ddp_write_relaxed(cmdq_pkt, hdr_pitch, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_HDR_PITCH(ovl, idx));
	} else {
		mtk_ddp_write_relaxed(cmdq_pkt, pitch_msb,
				      &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_PITCH_MSB(idx));
	}
}

void mtk_ovl_layer_config(struct device *dev, unsigned int idx,
			  struct mtk_plane_state *state,
			  struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	struct mtk_plane_pending_state *pending = &state->pending;
	unsigned int addr = pending->addr;
	unsigned int pitch_lsb = pending->pitch & GENMASK(15, 0);
	unsigned int fmt = pending->format;
	unsigned int rotation = pending->rotation;
	unsigned int offset = (pending->y << 16) | pending->x;
	unsigned int src_size = (pending->height << 16) | pending->width;
	unsigned int blend_mode = state->base.pixel_blend_mode;
	unsigned int ignore_pixel_alpha = 0;
	unsigned int con;

	if (!pending->enable) {
		mtk_ovl_layer_off(dev, idx, cmdq_pkt);
		return;
	}

	con = mtk_ovl_fmt_convert(ovl, state);
	if (state->base.fb) {
		/* DRM opacity is 16 bits; the native hardware field is eight. */
		con |= ovl->data->mt6765 ? state->base.alpha >> 8 :
			state->base.alpha & OVL_CON_ALPHA;

		/*
		 * For blend_modes supported SoCs, always enable alpha blending.
		 * For blend_modes unsupported SoCs, enable alpha blending when has_alpha is set.
		 */
		if (ovl->data->mt6765 || blend_mode || state->base.fb->format->has_alpha)
			con |= OVL_CON_AEN;

		/*
		 * Although the alpha channel can be ignored, CONST_BLD must be enabled
		 * for XRGB format, otherwise OVL will still read the value from memory.
		 * For RGB888 related formats, whether CONST_BLD is enabled or not won't
		 * affect the result. Therefore we use !has_alpha as the condition.
		 */
		if (blend_mode == DRM_MODE_BLEND_PIXEL_NONE || !state->base.fb->format->has_alpha)
			ignore_pixel_alpha = OVL_CONST_BLEND;
	}

	/*
	 * Treat rotate 180 as flip x + flip y, and XOR the original rotation value
	 * to flip x + flip y to support both in the same time.
	 */
	if (rotation & DRM_MODE_ROTATE_180)
		rotation ^= DRM_MODE_REFLECT_X | DRM_MODE_REFLECT_Y;

	if (rotation & DRM_MODE_REFLECT_Y) {
		con |= OVL_CON_VIRT_FLIP;
		addr += (pending->height - 1) * pending->pitch;
	}

	if (rotation & DRM_MODE_REFLECT_X) {
		con |= OVL_CON_HORZ_FLIP;
		if (ovl->data->mt6765 && state->base.fb)
			addr += pending->width * state->base.fb->format->cpp[0] - 1;
		else
			addr += pending->pitch - 1;
	}

	if (ovl->data->mt6765) {
		u32 clip = 0, width = pending->width;

		if (fmt == DRM_FORMAT_UYVY || fmt == DRM_FORMAT_YUYV) {
			/* Fetch complete chroma pairs and discard their extra edge pixels. */
			if (pending->src_x & 1) {
				addr -= 2;
				width++;
				clip |= OVL_CLIP_LEFT;
			}
			if (width & 1) {
				width++;
				clip |= OVL_CLIP_RIGHT;
			}
			src_size = pending->height << 16 | width;
		}
		mtk_ddp_write_relaxed(cmdq_pkt, clip, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_CLIP(idx));
		mtk_ddp_write_relaxed(cmdq_pkt, OVL_COLOR_ALPHA, &ovl->cmdq_reg,
				      ovl->regs, DISP_REG_OVL_CLR(idx));
		mtk_ddp_write_relaxed(cmdq_pkt, 0, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_SRCKEY(idx));
		mtk_ddp_write_relaxed(cmdq_pkt, 0, &ovl->cmdq_reg, ovl->regs,
				      DISP_REG_OVL_SECURE);
	}

	if (ovl->data->supports_afbc)
		mtk_ovl_set_afbc(ovl, cmdq_pkt, idx,
				 pending->modifier != DRM_FORMAT_MOD_LINEAR);

	mtk_ddp_write_relaxed(cmdq_pkt, con, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_CON(idx));
	mtk_ddp_write_relaxed(cmdq_pkt, pitch_lsb | ignore_pixel_alpha,
			      &ovl->cmdq_reg, ovl->regs, DISP_REG_OVL_PITCH(idx));
	mtk_ddp_write_relaxed(cmdq_pkt, src_size, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_SRC_SIZE(idx));
	mtk_ddp_write_relaxed(cmdq_pkt, offset, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_OFFSET(idx));
	mtk_ddp_write_relaxed(cmdq_pkt, addr, &ovl->cmdq_reg, ovl->regs,
			      DISP_REG_OVL_ADDR(ovl, idx));

	if (ovl->data->supports_afbc)
		mtk_ovl_afbc_layer_config(ovl, idx, pending, cmdq_pkt);

	mtk_ovl_set_bit_depth(dev, idx, fmt, cmdq_pkt);
	mtk_ovl_layer_on(dev, idx, cmdq_pkt);
}

void mtk_ovl_bgclr_in_on(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	unsigned int reg;

	reg = readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON);
	reg = reg | OVL_BGCLR_SEL_IN;
	writel(reg, ovl->regs + DISP_REG_OVL_DATAPATH_CON);
}

void mtk_ovl_bgclr_in_off(struct device *dev)
{
	struct mtk_disp_ovl *ovl = dev_get_drvdata(dev);
	unsigned int reg;

	reg = readl(ovl->regs + DISP_REG_OVL_DATAPATH_CON);
	reg = reg & ~OVL_BGCLR_SEL_IN;
	writel(reg, ovl->regs + DISP_REG_OVL_DATAPATH_CON);
}

static int mtk_disp_ovl_bind(struct device *dev, struct device *master,
			     void *data)
{
	return 0;
}

static void mtk_disp_ovl_unbind(struct device *dev, struct device *master,
				void *data)
{
}

static const struct component_ops mtk_disp_ovl_component_ops = {
	.bind	= mtk_disp_ovl_bind,
	.unbind = mtk_disp_ovl_unbind,
};

static int mtk_disp_ovl_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mtk_disp_ovl *priv;
	unsigned long irq_flags = IRQF_TRIGGER_NONE;
	int irq;
	int ret;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	irq = platform_get_irq(pdev, 0);
	if (irq < 0)
		return irq;
	priv->dev = dev;
	priv->irq = irq;

	priv->clk = devm_clk_get(dev, NULL);
	if (IS_ERR(priv->clk))
		return dev_err_probe(dev, PTR_ERR(priv->clk),
				     "failed to get ovl clk\n");

	priv->regs = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(priv->regs))
		return dev_err_probe(dev, PTR_ERR(priv->regs),
				     "failed to ioremap ovl\n");
#if IS_REACHABLE(CONFIG_MTK_CMDQ)
	ret = cmdq_dev_get_client_reg(dev, &priv->cmdq_reg, 0);
	if (ret)
		dev_dbg(dev, "get mediatek,gce-client-reg fail!\n");
#endif

	priv->data = of_device_get_match_data(dev);
	platform_set_drvdata(pdev, priv);
	if (priv->data->mt6765)
		irq_flags |= IRQF_NO_AUTOEN;

	ret = devm_request_irq(dev, irq, mtk_disp_ovl_irq_handler,
			       irq_flags, dev_name(dev), priv);
	if (ret < 0)
		return dev_err_probe(dev, ret, "Failed to request irq %d\n", irq);

	pm_runtime_enable(dev);

	ret = component_add(dev, &mtk_disp_ovl_component_ops);
	if (ret) {
		pm_runtime_disable(dev);
		return dev_err_probe(dev, ret, "Failed to add component\n");
	}

	return 0;
}

static void mtk_disp_ovl_remove(struct platform_device *pdev)
{
	component_del(&pdev->dev, &mtk_disp_ovl_component_ops);
	pm_runtime_disable(&pdev->dev);
}

static const struct mtk_disp_ovl_data mt2701_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT2701,
	.gmc_bits = 8,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = false,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8167_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 8,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8173_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 8,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8183_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8183_ovl_2l_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 2,
	.fmt_rgb565_is_0 = true,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8192_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.blend_modes = BIT(DRM_MODE_BLEND_PREMULTI) |
		       BIT(DRM_MODE_BLEND_COVERAGE) |
		       BIT(DRM_MODE_BLEND_PIXEL_NONE),
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8192_ovl_2l_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 2,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.blend_modes = BIT(DRM_MODE_BLEND_PREMULTI) |
		       BIT(DRM_MODE_BLEND_COVERAGE) |
		       BIT(DRM_MODE_BLEND_PIXEL_NONE),
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_ovl_data mt8195_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.supports_afbc = true,
	.blend_modes = BIT(DRM_MODE_BLEND_PREMULTI) |
		       BIT(DRM_MODE_BLEND_COVERAGE) |
		       BIT(DRM_MODE_BLEND_PIXEL_NONE),
	.formats = mt8195_formats,
	.num_formats = ARRAY_SIZE(mt8195_formats),
	.supports_clrfmt_ext = true,
};

static const struct mtk_disp_ovl_data mt6765_ovl_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 4,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.blend_modes = BIT(DRM_MODE_BLEND_PREMULTI) |
		       BIT(DRM_MODE_BLEND_COVERAGE) |
		       BIT(DRM_MODE_BLEND_PIXEL_NONE),
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
	.mt6765 = true,
};

static const struct mtk_disp_ovl_data mt6765_ovl_2l_driver_data = {
	.addr = DISP_REG_OVL_ADDR_MT8173,
	.gmc_bits = 10,
	.layer_nr = 2,
	.fmt_rgb565_is_0 = true,
	.smi_id_en = true,
	.blend_modes = BIT(DRM_MODE_BLEND_PREMULTI) |
		       BIT(DRM_MODE_BLEND_COVERAGE) |
		       BIT(DRM_MODE_BLEND_PIXEL_NONE),
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
	.mt6765 = true,
};

static const struct of_device_id mtk_disp_ovl_driver_dt_match[] = {
	{ .compatible = "mediatek,mt6765-disp-ovl",
	  .data = &mt6765_ovl_driver_data },
	{ .compatible = "mediatek,mt6765-disp-ovl-2l",
	  .data = &mt6765_ovl_2l_driver_data },
	{ .compatible = "mediatek,mt2701-disp-ovl",
	  .data = &mt2701_ovl_driver_data},
	{ .compatible = "mediatek,mt8167-disp-ovl",
	  .data = &mt8167_ovl_driver_data},
	{ .compatible = "mediatek,mt8173-disp-ovl",
	  .data = &mt8173_ovl_driver_data},
	{ .compatible = "mediatek,mt8183-disp-ovl",
	  .data = &mt8183_ovl_driver_data},
	{ .compatible = "mediatek,mt8183-disp-ovl-2l",
	  .data = &mt8183_ovl_2l_driver_data},
	{ .compatible = "mediatek,mt8192-disp-ovl",
	  .data = &mt8192_ovl_driver_data},
	{ .compatible = "mediatek,mt8192-disp-ovl-2l",
	  .data = &mt8192_ovl_2l_driver_data},
	{ .compatible = "mediatek,mt8195-disp-ovl",
	  .data = &mt8195_ovl_driver_data},
	{},
};
MODULE_DEVICE_TABLE(of, mtk_disp_ovl_driver_dt_match);

struct platform_driver mtk_disp_ovl_driver = {
	.probe		= mtk_disp_ovl_probe,
	.remove		= mtk_disp_ovl_remove,
	.driver		= {
		.name	= "mediatek-disp-ovl",
		.of_match_table = mtk_disp_ovl_driver_dt_match,
	},
};
