# pve_ceph_benchmark

Phase G/H. A **temporary, gated** benchmark of Ceph pool-tier variants (replica3 / EC2+1 × none / zstd-aggressive) using `rados bench` and `fio` librbd, followed by a **guarded teardown** that only ever removes pools carrying the `tst-` prefix.

This role touches production only by sharing the OSDs; it creates its own temporary pools and removes them again. It is gated behind `pve_ceph_benchmark_confirm: true` for the measurement half. The teardown half is guarded and safe, and is reachable on its own.

## Tags

- `--tags ceph_benchmark` — full cycle: create temp pools, run the measurement, tear the pools down. Requires `-e pve_ceph_benchmark_confirm=true`.
- `--tags ceph_benchmark_teardown` — teardown only, to clean up after a partial or aborted run. No confirmation required (it only deletes `tst-` pools).

## What It Does

1. **Pools (G1/G2).** Asserts every pool name starts with `tst-`, creates the `ec21` EC profile (k=2 m=1, host domain, class nvme), a replicated `tst-ec-meta` metadata pool for EC RBD images, and the four data pools (replica3 / EC2+1 × none/zstd). Waits for `HEALTH_OK`.
2. **Measure (G).** Renders `ceph_benchmark.sh` onto the first node and runs it: each image is **prefilled** (so fio reads are valid), then `rados bench` (write/seq/rand, t=16/64) and `fio` librbd (4K randread/randwrite/70-30, 1M seq r/w) run for `pve_ceph_benchmark_rounds` rounds. Results land in an on-node bundle.
3. **Teardown (H).** Reads `mon_allow_pool_delete`, sets it true, deletes only `tst-` pools (double-guarded), removes the EC profile, restores `mon_allow_pool_delete`, and asserts no `tst-` pool survived.

## Preserved Gotchas / Caveats

- **`mon_allow_pool_delete`** must be true for `ceph osd pool delete`; the role flips it on for the teardown window and restores it.
- **EC `min_size = k + 1`.** EC2+1 has `min_size` 3, so one shard loss blocks I/O — EC is a capacity tier, not a VM/random tier.
- **fio read validity.** Reads on a freshly created sparse image return zero-fill (a hand-run once reported 159 GB/s); every image is prefilled before read measurement.
- **Hyperconverged/shared OSDs.** Numbers are relative comparisons between tiers, not absolute maxima.

## Key Variables

- `pve_ceph_benchmark_confirm` (default `false`) — the human gate.
- `pve_ceph_benchmark_pools` — list of `{label, name, type, compression, meta}`; all names must start with `pve_ceph_benchmark_pool_prefix` (`tst-`).
- `pve_ceph_benchmark_rounds` (4), `pve_ceph_benchmark_image_size` (`256G`), `pve_ceph_benchmark_rados_secs` (20), `pve_ceph_benchmark_fio_runtime` (60).
- `pve_ceph_benchmark_ec_profile`/`_ec_k`/`_ec_m`, `pve_ceph_benchmark_pg_num`, `pve_ceph_benchmark_meta_pool`.

## Long Runs

The measurement is tens of minutes. It runs `async` so a transient control-connection drop does not abandon it; for a production run over an unstable link, launch it inside `screen` on the node (project practice: run long tasks detached on DC nodes).

## Report

Emits a change report through `tools/report.py` (`ceph-benchmark-change.json` → Markdown + HTML), with verification facts for post-run health, absence of `tst-` pools, and the result-bundle location.
