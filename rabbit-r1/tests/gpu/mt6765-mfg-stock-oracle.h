/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Copyright (c) 2020 MediaTek Inc.
 * Author: Owen Chen <owen.chen@mediatek.com>
 *
 * MFG bus-protection excerpt from the pinned Rabbit MT6765 driver.
 * Used only with host register mocks; no device I/O is performed.
 * Full source identity and extraction scope: mt6765-power-contract.json.
 */
#define STA_POWER_DOWN	0
#define INFRA_TOPAXI_PROTECTEN_SET	INFRACFG_REG(0x02A0)
#define INFRA_TOPAXI_PROTECTEN_STA1	INFRACFG_REG(0x0228)
#define INFRA_TOPAXI_PROTECTEN_CLR	INFRACFG_REG(0x02A4)
#define MFG_PWR_CON			SPM_REG(0x0338)
#define MFG_PROT_STEP1_0_MASK		((0x1 << 25))
#define MFG_PROT_STEP1_0_ACK_MASK	((0x1 << 25))
#define MFG_PROT_STEP2_0_MASK		((0x1 << 21) 					|(0x1 << 22))
#define MFG_PROT_STEP2_0_ACK_MASK	((0x1 << 21) 					|(0x1 << 22))
#define MFG_SRAM_PDN			(0x1 << 8)
#define MFG_SRAM_PDN_ACK		(0x1 << 12)
#define MFG_SRAM_PDN_ACK_BIT0		(0x1 << 12)
static int spm_mtcmos_ctrl_mfg_bus_prot(int state)
{
	int err = 0;

	DBG_ID = DBG_ID_MFG_BUS;
	DBG_STA = state;
	DBG_STEP = 0;

	/* TINFO="enable SPM register control" */

	if (state == STA_POWER_DOWN) {
		/* TINFO="Start to turn off MFG" */
		/* TINFO="Set bus protect - step1 : 0" */
		spm_write(INFRA_TOPAXI_PROTECTEN_SET, MFG_PROT_STEP1_0_MASK);
#ifndef IGNORE_MTCMOS_CHECK
		while ((spm_read(INFRA_TOPAXI_PROTECTEN_STA1)
			& MFG_PROT_STEP1_0_ACK_MASK)
			!= MFG_PROT_STEP1_0_ACK_MASK) {
			ram_console_update();
		}
		INCREASE_STEPS;
#endif
		/* TINFO="Set bus protect - step2 : 0" */
		spm_write(INFRA_TOPAXI_PROTECTEN_SET, MFG_PROT_STEP2_0_MASK);
#ifndef IGNORE_MTCMOS_CHECK
		while ((spm_read(INFRA_TOPAXI_PROTECTEN_STA1)
			& MFG_PROT_STEP2_0_ACK_MASK)
			!= MFG_PROT_STEP2_0_ACK_MASK) {
			ram_console_update();
		}
		INCREASE_STEPS;
#endif
		/* TINFO="Set SRAM_PDN = 1" */
		spm_write(MFG_PWR_CON, spm_read(MFG_PWR_CON) | MFG_SRAM_PDN);
#ifndef IGNORE_MTCMOS_CHECK
		/* TINFO="Wait until MFG_SRAM_PDN_ACK = 1" */
		while ((spm_read(MFG_PWR_CON) & MFG_SRAM_PDN_ACK)
			!= MFG_SRAM_PDN_ACK)
			ram_console_update();
		/* Need f_fmfg_core_ck for SRAM PDN delay IP. */
#endif
	} else {    /* STA_POWER_ON */
		spm_write(MFG_PWR_CON, spm_read(MFG_PWR_CON) & ~(0x1 << 8));
#ifndef IGNORE_MTCMOS_CHECK
		/* TINFO="Wait until MFG_SRAM_PDN_ACK_BIT0 = 0" */
		while (spm_read(MFG_PWR_CON) & MFG_SRAM_PDN_ACK_BIT0)
			ram_console_update();
		/* Need f_fmfg_core_ck for SRAM PDN delay IP. */
		INCREASE_STEPS;
#endif
		/* TINFO="Release bus protect - step2 : 0" */
		spm_write(INFRA_TOPAXI_PROTECTEN_CLR, MFG_PROT_STEP2_0_MASK);
#ifndef IGNORE_MTCMOS_CHECK
		/* Note that this protect ack check after
		 * releasing protect has been ignored
		 */
#endif
		/* TINFO="Release bus protect - step1 : 0" */
		spm_write(INFRA_TOPAXI_PROTECTEN_CLR, MFG_PROT_STEP1_0_MASK);
#ifndef IGNORE_MTCMOS_CHECK
		/* Note that this protect ack check after
		 * releasing protect has been ignored
		 */
#endif
		/* TINFO="Finish to turn on MFG" */
	}
	INCREASE_STEPS;
	ram_console_update();
	return err;
}
