# Rabbit r1 boot notes review

[DavidBuchanan314/rabbit_r1_boot_notes](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes)
is useful evidence for the loader handoff and early debugging. The reviewed
revision is `b1b35b1ea9023d7b38df407afb5911fd54287c2f` (2024-11-26), cloned locally
at `/rabbitr1/src/rabbit-r1-boot-notes`. Its observations predate the supplied
RabbitOS v0.8.293 firmware. No device operations were run during this review.

## DTBO affects LK before Linux

The [patched UART capture](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes/blob/b1b35b1ea9023d7b38df407afb5911fd54287c2f/uart_boot_logs_with_patch.txt#L672-L705)
shows LK loading `lk_main_dtb` from its own image, applying the selected DTBO,
then looking up `/gpio@10005000` and its 5,040-byte initialization table. This
happens before the Linux handoff. That lookup alone does not prove the mainline
kernel DT needs a second GPIO-controller node at the root.

An offline check against **our v0.8.293 images** found:

* `lk.img` contains a valid 103,765-byte FDT at file offset 756,496.
* That FDT is byte-identical to the stock boot image's base DTB.
* The stock overlay adds `gpio_init_default` (5,040 bytes). It also changes
  MT6370 charger properties and PMIC interrupt configuration.

The old no-op DTBO dropped those additions from LK's private DT. The package
now retains the complete stock DTBO and patches only the Linux overlay call
site in the exact v0.8.293 LK. Its shared overlay function and early board setup
remain unchanged. See [LK.md](LK.md) for the instruction changes and emulation
checks. Selected later reservation writers now run in emulation with controlled
boot arguments; the live memory map and hardware boot remain untested.

## Logs and reserved memory

The UART capture independently records `expdb` at offset `0x188000`, with size
`0x1400000` (20 MiB), matching our stock scatter. An LK log-store write near the
partition end supports excluding that tail from our pstore mapping.

Its [memory map](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes/blob/b1b35b1ea9023d7b38df407afb5911fd54287c2f/android_proc_iomap.txt)
and UART output show persistent RAM and firmware reservations missing from a
static boot DT alone. These are leads for early-crash logging, not addresses to
hard-code into the v0.8.293 mainline DT. Capture the actual final FDT and confirm
which reservations survive reset before using a RAM logging profile.

## RAM booting as a development option

The [LK hook](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes/blob/b1b35b1ea9023d7b38df407afb5911fd54287c2f/scripts/custom_da/lk_hook.c)
and [WebSerial instructions](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes/blob/b1b35b1ea9023d7b38df407afb5911fd54287c2f/scripts/webusb/README.md)
describe a tethered replacement boot image held in RAM. That is a useful
direction for short bring-up cycles once adapted to the exact firmware. The
documented tests used RabbitOS v0.8.107/v0.8.112, and the code includes hard-coded
preloader addresses. It is not a verified drop-in loader for v0.8.293 or this
mainline build. The prototype's Android root-shell payload is not needed for
our diagnostic initramfs.

These sources improve the boot investigation; they do not provide mainline
charger, GPU, wireless or other missing peripheral drivers.

## Panel power readings

The same [UART capture](https://github.com/DavidBuchanan314/rabbit_r1_boot_notes/blob/b1b35b1ea9023d7b38df407afb5911fd54287c2f/uart_boot_logs_with_patch.txt#L801-L812)
records MT6370 display-bias reads `B0=00`, `B1=32` and `B3=5e`, followed by a
DSI `0x0a` response of `0x9c`. Both software bias-enable bits in `B1` and the
external-control bit in `B0` are clear at the recorded read. Those observations
do not identify the panel supplies or establish v0.8.293 hardware state.
The [panel power audit](DISPLAY.md#power-inherited-from-firmware) independently
executes v0.8.293's matching read-only LK callback and the shipped kernel's
no-op bias helpers. Generic MT6370 DSV nodes are therefore insufficient evidence
for assigning panel supplies.
