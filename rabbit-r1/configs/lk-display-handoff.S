/* RabbitOS v0.8.293 only. Raw LK offsets include its 512-byte header.
 * The relock guard tail-calls fastboot_fail from 0x2878a, freeing this tail.
 * Link .text at 0x287c8; all LK calls and context references are PC-relative.
 * Keep clocks/panel power on. Never proceed after unsupported state or timeout.
 */
.syntax unified
.thumb
.arch armv7-a
.section .text,"ax",%progbits
.global handoff
.type handoff,%function
.thumb_func
handoff:
.Lstart:
    push {r3,r4,r5,lr}
    cpsid if                 /* No interrupt may restart the stopped path. */
    ldr r4, context_rel
context_pc:
    add r4, pc
    ldr r0, [r4]             /* Primary context initialized. */
    cbz r0, failed
    ldr r0, [r4, #0x10]      /* Display initialized. */
    cbz r0, failed
    ldr r0, [r4, #0x30]      /* Primary path handle. */
    cbz r0, failed
    ldr r1, [r0, #8]         /* Path powered. */
    cbz r1, failed
    ldr r1, [r0, #0xd8]      /* Scenario 0; excludes WDMA paths. */
    cmp r1, #0
    bne failed
    bl lk_path_stop          /* r1=0: immediate, no command queue. */
    ldr r3, rdma_global
    ldr r3, [r3]
    tst r3, #3               /* RDMA disabled and direct-input mode only. */
    bne failed
    movs r5, #0
    ldr r4, ovl_flow
ovl:
    mov r0, r5
    bl lk_ovl_reset
    ldr r3, [r4]             /* Stock reset falsely returns 0 on timeout. */
    tst r3, #3
    beq failed
    add.w r4, r4, #0x1000
    adds r5, #1
    cmp r5, #2
    blo ovl
    movs r0, #17
    bl lk_dsi_reset
    ldr r4, dsi_status
    movw r5, #2000
wait_dsi:
    ldr r3, [r4]
    cmp r3, #0
    bpl finished             /* DSI BUSY bit 31 cleared after reset. */
    movs r0, #10
    bl lk_udelay
    subs r5, #1
    bne wait_dsi
failed:
    adr r0, message
    bl lk_printf
halt:
    dsb sy
    wfi                      /* Halt until recovery or watchdog reset. */
    b halt
finished:
    dsb sy
    pop {r3,r4,r5,lr}
    b.w lk_gic_disable
.balign 4
context_rel: .word 0xbb200 - 0x287c8 - (context_pc - .Lstart) - 4
rdma_global: .word 0x1400d010
ovl_flow: .word 0x1400b240
dsi_status: .word 0x1401400c
message: .asciz "R1: DMA handoff failed\n"
.balign 4
/* Also fails assembly if a future edit exceeds the free 176-byte slot. */
.space 176-(.-.Lstart), 0
.size handoff, .-handoff
.thumb_set lk_path_stop, 0x8c29
.thumb_set lk_ovl_reset, 0xa5e9
.thumb_set lk_dsi_reset, 0xc875
.thumb_set lk_udelay, 0x4a519
.thumb_set lk_printf, 0x2a3bd
.thumb_set lk_gic_disable, 0x2d85
