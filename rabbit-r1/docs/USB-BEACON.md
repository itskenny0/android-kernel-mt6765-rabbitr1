# One-shot USB initramfs beacon

This optional diagnostic tests whether PID1 reaches the existing USB gadget
path when descriptors fail. It is not an early kernel console or a USB fix.
Normal boots are unchanged. Enable only with both `r1.usb=1` and the exact
kernel command-line token `r1.usb_beacon=1` in a separately reviewed boot image.
The normal package command line does not enable it.

After gadget setup and bounded UDC discovery, the worker waits 20 seconds,
binds the controller once and verifies the configfs name, waits 10 seconds,
then unbinds and verifies the name is empty. It stays unbound: no getty, rebind,
retry or reboot follows. UART/PID1 shell and the expdb logger still run
independently. Normal ACM can return only through another normal boot.

The first wait does not prove electrical disconnect: inherited loader pullup
state is unknown. A matched transition and final quiet period of at least
30 seconds would provide positive evidence for the init path, if distinguished
from the host's ordinary enumeration retries. Missing transitions remain
ambiguous. Software bind/readback does not prove host enumeration; unbind
readback does not prove electrical disconnect completed or bound its latency.
The MUSB pullup update uses deferred work.

Each beacon phase records `/proc/uptime` in `/run/usb-beacon.log`, stdout captured
by `/run/usb-start.log`, and printk. `/run/usb-start.status` retains the last
status. Existing pstore may capture printk if storage logging works; none of
these RAM logs survive reset by themselves. Preserve host timestamps across
the full attempt and compare against recovered phase logs if available.

Every wait, bind, unbind and name-readback failure stops the sequence with a
specific status. A failure after binding may leave the gadget bound or in an
unknown state; no automatic recovery is attempted. Kernel calls themselves
can block. This is a finite shell sequence, not a hard real-time guarantee.

Repeated registration is intentionally avoided: the current MUSB stop path
notes that registering another gadget can misbehave. The generic `soft_connect`
attribute also stop/starts the UDC and ignores internal return values, so it
is not used.

`test-initramfs-usb-beacon.py --out NEW_DIRECTORY` runs the original 12 USB
controls unchanged plus 11 beacon controls in each available shell. It uses
fake configfs, time, mount and getty operations; it does not test physical USB.
