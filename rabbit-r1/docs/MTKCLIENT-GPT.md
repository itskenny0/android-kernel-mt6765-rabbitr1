# Complete primary GPT export

The pinned mtkclient's protective-MBR GPT path read a fixed 32 sectors after
the primary-header offset. With a header at LBA 1 and an entry array at LBA 2,
128 entries of 128 bytes require 34 sectors from the start of the disk. The old
export contained only 33 sectors (16,896 bytes with 512-byte sectors), causing
the diagnostic helper's strict GPT parser to reject the truncated array before
any write.

The patched `Partition.parse_mbr()` derives the export end from the parsed
header's array LBA, entry count and entry size, then rounds to a complete sector.
It rejects invalid/overlapping array bounds, an export beyond the device size,
and a short final read. The existing partition parser and diagnostic helper's
header CRC, array CRC, range and truncation checks are unchanged. This corrects
the primary GPT export path, not the unrelated MBR/PMT/BPI paths or all generic
GPT parsing behavior.

`gpt_backup.bin` remains the upstream name for the primary export with its first
sector removed. **It is not a separately read physical backup GPT.** Preserve an
independently captured final-sector header and its entry array when a complete
GPT backup is required. This fix adds no storage write or additional backup-GPT
wire command, and does not make a GPT layout a unique device identity.

The transport manifest now pins six modified upstream files. The existing
five-file transfer/CID patch is preserved; `partition.py` is the sixth file.
The offline preparer rejects the old five-file manifest rather than silently
installing an incomplete correction.

Run the focused synthetic check against prepared sources:

```sh
python3 scripts/test-mtkclient-gpt.py \
  --source /rabbitr1/src/mtkclient-haretic \
  --original /rabbitr1/src/mtkclient \
  --out /rabbitr1/out/mtkclient-gpt-tests
```

It executes actual Partition, MBR/GPT, MTKFileHandler and struct-reader bodies
with an in-memory readflash boundary, plus the actual strict diagnostic parser.
Controls cover the original truncation, changed array locations/counts/strides,
sector rounding, invalid bounds, short/error reads, and unchanged CRC rejection.
No USB modules are imported or device commands executed. Optional private capture
arguments support a local reproduction without publishing device data.
