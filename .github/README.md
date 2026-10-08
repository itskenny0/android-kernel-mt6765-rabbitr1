# haretic

haretic is a mainline Linux and LineageOS firmware project for the rabbit r1.
Development takes place on `rabbit-r1/bringup`.

The kernel and diagnostic boot package build and pass offline checks. The
Android port is unfinished, and no device boot has been tested. These artifacts
are not ready for beta testers.

- [Build instructions and current status](../rabbit-r1/README.md)
- [Android integration](../rabbit-r1/docs/ANDROID.md)
- [Flashing, backups and restoration](../rabbit-r1/docs/FLASHING.md)
- [GitHub Actions](../rabbit-r1/docs/CI.md)

The build uses [mtklkzap](https://github.com/itskenny0/mtklkzap), an independent
project maintained by itskenny0 outside the haretic organization.

Do not relock the bootloader until the complete stock firmware package,
including every LK slot, has been restored.
