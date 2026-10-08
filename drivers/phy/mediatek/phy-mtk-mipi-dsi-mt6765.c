// SPDX-License-Identifier: GPL-2.0-only
/* MT6765 D-PHY sequence from MediaTek's published MT6765 display driver. */

#include <linux/math64.h>

#include "phy-mtk-io.h"
#include "phy-mtk-mipi-dsi.h"

#define MIPITX_LANE_CON		0x00c
#define MIPITX_PLL_PWR		0x028
#define SDM_PWR_ON		BIT(0)
#define SDM_ISO_EN		BIT(1)
#define MIPITX_PLL_CON0		0x02c
#define MIPITX_PLL_CON1		0x030
#define PLL_EN			BIT(4)
#define PLL_POSDIV		GENMASK(10, 8)
#define MIPITX_PLL_CON2		0x034
#define SSC_EN			BIT(1)
#define MIPITX_PLL_CON4		0x03c
#define MIPITX_SW_CTRL_CON4	0x060
#define SW_ANA_CK_EN		BIT(8)

/* Physical bank order: D2, D0, CK, D1, D3. */
#define MIPITX_LANE_BASE(n)	(0x100 * ((n) + 1))
#define LANE_CKMODE_EN		0x28
#define LANE_SW_CTL_EN		0x44
#define LANE_SW_LPTX_PRE_OE	0x48
#define LANE_SW_LPTX_DN		0x54
#define LANE_BIT			BIT(0)

#define MT6765_REF_RATE		26000000
#define MT6765_MIN_RATE		125000000
#define MT6765_MAX_RATE		2500000000U

static const u8 mt6765_lane_order[] = { 1, 3, 0, 4, 2 };

static int mt6765_mipi_tx_pll_enable(struct clk_hw *hw)
{
	struct mtk_mipi_tx *tx = mtk_mipi_tx_from_clk_hw(hw);
	struct clk_hw *parent = clk_hw_get_parent(hw);
	void __iomem *base = tx->regs;
	u32 rate = tx->data_rate;
	u32 posdiv;
	u64 pcw;

	if (rate < MT6765_MIN_RATE || rate > MT6765_MAX_RATE || !parent ||
	    clk_hw_get_rate(parent) != MT6765_REF_RATE)
		return -EINVAL;

	if (rate >= 2000000000)
		posdiv = 0;
	else if (rate >= 1000000000)
		posdiv = 1;
	else if (rate >= 500000000)
		posdiv = 2;
	else if (rate > 250000000)
		posdiv = 3;
	else
		posdiv = 4;

	mtk_phy_set_bits(base + MIPITX_PLL_PWR, SDM_PWR_ON);
	udelay(2);
	mtk_phy_clear_bits(base + MIPITX_PLL_PWR, SDM_ISO_EN);

	pcw = div_u64(((u64)rate << posdiv) << 24, MT6765_REF_RATE);
	writel(pcw, base + MIPITX_PLL_CON0);
	mtk_phy_update_field(base + MIPITX_PLL_CON1, PLL_POSDIV, posdiv);
	/* The r1 panel requests no spread-spectrum modulation. */
	mtk_phy_clear_bits(base + MIPITX_PLL_CON2, SSC_EN);
	mtk_phy_set_bits(base + MIPITX_PLL_CON1, PLL_EN);
	udelay(50);
	mtk_phy_set_bits(base + MIPITX_SW_CTRL_CON4, SW_ANA_CK_EN);

	return 0;
}

static void mt6765_mipi_tx_pll_disable(struct clk_hw *hw)
{
	struct mtk_mipi_tx *tx = mtk_mipi_tx_from_clk_hw(hw);
	void __iomem *base = tx->regs;

	mtk_phy_clear_bits(base + MIPITX_PLL_CON1, PLL_EN);
	mtk_phy_clear_bits(base + MIPITX_SW_CTRL_CON4, SW_ANA_CK_EN);
	mtk_phy_set_bits(base + MIPITX_PLL_PWR, SDM_ISO_EN);
	mtk_phy_clear_bits(base + MIPITX_PLL_PWR, SDM_PWR_ON);
}

static int mt6765_mipi_tx_pll_determine_rate(struct clk_hw *hw,
					  struct clk_rate_request *req)
{
	req->rate = clamp_val(req->rate, MT6765_MIN_RATE, MT6765_MAX_RATE);
	return 0;
}

static int mt6765_mipi_tx_pll_set_rate(struct clk_hw *hw, unsigned long rate,
				     unsigned long parent_rate)
{
	if (rate < MT6765_MIN_RATE || rate > MT6765_MAX_RATE ||
	    parent_rate != MT6765_REF_RATE)
		return -EINVAL;

	return mtk_mipi_tx_pll_set_rate(hw, rate, parent_rate);
}

static const struct clk_ops mt6765_mipi_tx_pll_ops = {
	.enable = mt6765_mipi_tx_pll_enable,
	.disable = mt6765_mipi_tx_pll_disable,
	.determine_rate = mt6765_mipi_tx_pll_determine_rate,
	.set_rate = mt6765_mipi_tx_pll_set_rate,
	.recalc_rate = mtk_mipi_tx_pll_recalc_rate,
};

static void mt6765_mipi_tx_lanes_off(struct mtk_mipi_tx *tx)
{
	unsigned int i, offset;
	void __iomem *lane;

	/* Set LP00 before selecting software control of the pads. */
	for (i = 0; i < ARRAY_SIZE(tx->rt_code); i++) {
		lane = tx->regs + MIPITX_LANE_BASE(i);
		for (offset = LANE_SW_LPTX_PRE_OE; offset <= LANE_SW_LPTX_DN;
		     offset += 4)
			mtk_phy_clear_bits(lane + offset, LANE_BIT);
	}
	for (i = 0; i < ARRAY_SIZE(mt6765_lane_order); i++)
		mtk_phy_set_bits(tx->regs + MIPITX_LANE_BASE(mt6765_lane_order[i]) +
				 LANE_SW_CTL_EN, LANE_BIT);

	/* Stock tie-low and bandgap shutdown values, including preserve bits. */
	writel(0x3fff0180, tx->regs + MIPITX_LANE_CON);
	writel(0x3fff0100, tx->regs + MIPITX_LANE_CON);
}

static int mt6765_mipi_tx_prepare_signal(struct phy *phy)
{
	struct mtk_mipi_tx *tx = phy_get_drvdata(phy);
	unsigned int i, bit;
	void __iomem *lane;
	int ret;

	if (tx->data_rate < MT6765_MIN_RATE || tx->data_rate > MT6765_MAX_RATE ||
	    clk_get_rate(tx->ref_clk) != MT6765_REF_RATE)
		return -EINVAL;

	/* Keep the reference gated on through pre-PLL setup and post-PLL teardown. */
	ret = clk_prepare_enable(tx->ref_clk);
	if (ret)
		return ret;

	if (!tx->rt_code_saved) {
		/* Stock Linux snapshots the calibration programmed by LK. */
		for (i = 0; i < ARRAY_SIZE(tx->rt_code); i++) {
			lane = tx->regs + MIPITX_LANE_BASE(i);
			tx->rt_code[i] = 0;
			for (bit = 0; bit < 10; bit++)
				tx->rt_code[i] |= (readl(lane + bit * 4) & 1) << bit;
		}
		tx->rt_code_saved = true;
	}

	/* Stop an inherited firmware link before changing its analog setup. */
	if (readl(tx->regs + MIPITX_PLL_CON1) & PLL_EN) {
		mt6765_mipi_tx_pll_disable(&tx->pll_hw);
		mt6765_mipi_tx_lanes_off(tx);
	}

	mtk_phy_set_bits(tx->regs + MIPITX_LANE_BASE(2) + LANE_CKMODE_EN, LANE_BIT);
	for (i = 0; i < ARRAY_SIZE(tx->rt_code); i++) {
		lane = tx->regs + MIPITX_LANE_BASE(i);
		for (bit = 0; bit < 10; bit++)
			writel((tx->rt_code[i] >> bit) & 1, lane + bit * 4);
	}

	writel(0x00ff12e0, tx->regs + MIPITX_PLL_CON4);
	writel(0x3fff0180, tx->regs + MIPITX_LANE_CON);
	usleep_range(500, 600);
	writel(0x3fff0080, tx->regs + MIPITX_LANE_CON);
	for (i = 0; i < ARRAY_SIZE(mt6765_lane_order); i++)
		mtk_phy_clear_bits(tx->regs + MIPITX_LANE_BASE(mt6765_lane_order[i]) +
				   LANE_SW_CTL_EN, LANE_BIT);

	return 0;
}

static void mt6765_mipi_tx_unprepare_signal(struct phy *phy)
{
	struct mtk_mipi_tx *tx = phy_get_drvdata(phy);

	mt6765_mipi_tx_lanes_off(tx);
	clk_disable_unprepare(tx->ref_clk);
}

const struct mtk_mipitx_data mt6765_mipitx_data = {
	.mipi_tx_clk_ops = &mt6765_mipi_tx_pll_ops,
	.mipi_tx_prepare_signal = mt6765_mipi_tx_prepare_signal,
	.mipi_tx_unprepare_signal = mt6765_mipi_tx_unprepare_signal,
	.preserve_fw_calibration = true,
};
