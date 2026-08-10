# Server Results Guide

Everything the **Server** writes to disk during a run: what each file is, who writes it,
when, the exact line format, and how to read it.

All paths are relative to `log-path` in [config.yaml](config.yaml) (currently `.`, i.e. the
project root). Source references point at [src/Server.py](src/Server.py).

---

## 1. Lifecycle of a run

```
__init__            truncate all result logs  +  delete stale metrics/pred files
   │
START broadcast     _fps_start_t = now                          (t0 for SYSTEM FPS)
   │
run                 batch_done_ns.log      ← every DONE          (live, append)
                    fps_cluster_ns.log     ← every DONE          (live, append)
                    cut_change_ns.log      ← every cut change     (adaptive only)
   │
all edges done      FPS drain watcher (grace + hard cap)
   │
_finish_fps         console FPS summary  +  fps_cluster.log
   │
_collect_utilization        utilization.log
   └ _report_cluster_util_latency   utilization_cluster.log, latency_cluster.log
   │
_collect_map_pred   map/pred_collected/<cluster>/*.txt   (unpacked zips)
   ├ pipeline 1     map_window.log
   └ pipeline 2     map.log
   │
_archive_results    results/results_<MMDD>_<HHMM>_<tag>/   (copy of everything + config.yaml)
   │
connection.close() → exit
```

Two properties hold for every file below:

- **Truncated at startup**, in [Server.py:229-266](src/Server.py#L229-L266) — a new run never
  mixes its numbers with the previous one. (One exception: `cut_change_ns.log`, see §2.9.)
- **Timestamped with `time.time_ns()` on the server's clock only** — device clock skew never
  affects any recorded time. The single exception is the E2E latency samples, which span two
  machines by definition (§2.7).

---

## 2. The result files

Nine every run writes, plus five a run writes only when it measured the thing.

| File | Written by | When | Granularity | Always |
|---|---|---|---|---|
| `batch_done_ns.log` | `on_fps` | live | one line per finished batch | yes |
| `fps_cluster_ns.log` | `on_fps` | live | one line per finished batch, cluster-tagged | yes |
| `fps_cluster.log` | `_report_cluster_fps` | shutdown | one line per cluster + `SYSTEM` | yes |
| `utilization.log` | `_collect_utilization` | shutdown | one line per device | yes |
| `utilization_cluster.log` | `_report_cluster_util_latency` | shutdown | per cluster, per cluster/role, + `SYSTEM` | yes |
| `latency_cluster.log` | `_report_cluster_util_latency` | shutdown | per cluster/role (service, pipeline) + per cluster (e2e) + `SYSTEM` | yes |
| `map_window.log` | `_map_pipeline_window` | shutdown | one line per sliding window | yes |
| `map.log` | `_collect_map_pred` | shutdown | 2 lines per cluster + 2 `OVERALL` | yes |
| `cut_change_ns.log` | `_broadcast_setcut` | live | one line per cut change (adaptive only) | yes |
| `free_time.log` | free-time collector | shutdown | one line per device | optional |
| `free_time_cluster.log` | free-time collector | shutdown | per cluster, per cluster/role, per machine, + `SYSTEM` | optional |
| `free_time_series.log` | free-time collector | shutdown | one line per device per time bucket | optional |
| `broker_ram_ns.log` | the RAM sampler | live | one line per sample of the queue host | optional |
| `broker_ram.log` | the RAM sampler | shutdown | `BROKER` / `USED` / `DELTA` / `RABBIT` | optional |
| `message_size.log` | the measured worker | shutdown | one line per measured worker (normally one) | optional |
| `message_size_series.log` | the measured worker | shutdown | one line per published message | optional |

**The three optional features are all-or-nothing.** `free_time*` is one feature of three files
(§2.10–2.12), `broker_ram*` is one feature of two (§2.13–2.14), and `message_size*` is one
feature of two (§2.15–2.16) — a run emits all of a feature's files or none of them. Absence
means "this run did not measure it", which is not a fault and is not warned about; what *is*
worth reporting is an attempt that failed, and §2.14 has a line kind for exactly that.

### 2.1 `batch_done_ns.log` — system throughput series

Written in [Server.py:379-383](src/Server.py#L379-L383). One line per `DONE` message arriving
on `fps_queue`; the **arrival is the event**, the body is not used for timing.

```
1785127877009606331
1785127877009606331 24.13
```

| Field | Meaning |
|---|---|
| col 1 | ns-epoch arrival time of the DONE (server clock) |
| col 2 | smoothed `window_fps` over the last 16 batches — *absent* for the first 15 lines |

`window_fps = (W-1) * batch_size / (t[-1] - t[-W])`, `W = 16`. This is the system-wide
live throughput series — the plottable "FPS over time" curve. Format is deliberately
two-column and stable, so existing parsers keep working.

### 2.2 `fps_cluster_ns.log` — per-cluster throughput series

Written in [Server.py:402-406](src/Server.py#L402-L406). Same arrivals as above, but bucketed
by the cluster id carried in the DONE body.

```
1785127877009606331 cluster=intermediate_queue_0 done=1
1785127901123456789 cluster=intermediate_queue_0 done=16 window_fps=21.44
```

| Field | Meaning |
|---|---|
| `cluster` | producing cluster's queue name; `unknown` if a producer sent a bare `DONE` |
| `done` | running count of DONEs *for that cluster* |
| `window_fps` | that cluster's own 16-batch window fps — absent until it has 16 DONEs |

The system list in §2.1 stays authoritative: a mis-tagged DONE can shift this breakdown but
can never change the system total.

### 2.3 `fps_cluster.log` — final throughput summary

Written once by `_report_cluster_fps` in [Server.py:490-492](src/Server.py#L490-L492).

```
1785128504858762215 cluster=intermediate_queue_0 fps=18.131 steady_fps=18.491 done=336 frames=10752 share=66.7%
1785128504858762215 cluster=intermediate_queue_1 fps=8.407 steady_fps=8.740 done=168 frames=5376 share=33.3%
1785128504858762215 SYSTEM fps=25.220 done=504 frames=16128 clusters=2
```

| Field | Meaning |
|---|---|
| `fps` | whole-run rate: `frames / (START → that cluster's last DONE)`. **Additive** — cluster values sum to ≈ the SYSTEM value |
| `steady_fps` | `(n-1)*bs / (first DONE → last DONE)` — drops warm-up; the fair number for comparing clusters |
| `done` / `frames` | batches, and `batches × batch_size` |
| `share` | that cluster's % of all batches — the quickest read on whether the Hungarian assignment balanced the clusters |

The SYSTEM line has no `steady_fps`; system steady-state and the biased `ref mean` are printed
to the console only ([Server.py:516-528](src/Server.py#L516-L528)).

### 2.4 `utilization.log` — per-device busy ratio

Written in [Server.py:570-571](src/Server.py#L570-L571) as each device's `UTILIZATION` report is
drained from `utilization_queue` at shutdown.

```
1785128504862965418 client=7e3dd352-8b9a-4d6b-8173-412705d9bbe3 role=edge packages=56 busy_s=176.760 total_s=467.394 utilization=37.82%
```

| Field | Meaning |
|---|---|
| col 1 | ns-epoch **arrival** of the report (not a device timestamp) |
| `client` | device UUID |
| `role` | `edge` / `cloud` |
| `packages` | batches this device processed |
| `busy_s` / `total_s` | seconds computing vs. seconds alive, from the device's own timing log |
| `utilization` | `busy/total` — a low edge value with a high cloud value means the cut is too shallow |

If not every registered client reports within `timeout_s` (default 30 s), the collection is
partial, a yellow warning prints, and the run still shuts down cleanly.

### 2.5 `utilization_cluster.log` — utilization rolled up per cluster

Written in [Server.py:694-702](src/Server.py#L694-L702). Three line kinds:

```
1785128504874248492 cluster=intermediate_queue_0 ALL devices=8 utilization=55.06% utilization_mean=53.29% busy_s=2348.297 total_s=4264.621 packages=672
1785128504874248492 cluster=intermediate_queue_0 role=cloud devices=2 utilization=93.44% busy_s=1119.832 total_s=1198.399 packages=336
1785128504874248492 SYSTEM devices=11 clusters=2 utilization=... utilization_mean=... busy_s=... total_s=...
```

- `utilization` — **pooled**: `Σbusy / Σtotal`, weighting each device by how long it ran.
- `utilization_mean` — plain mean of the per-device ratios. Present on `ALL`/`SYSTEM` lines
  because a pooled number can hide one idle device inside a group of busy ones. When the two
  diverge, the group is imbalanced.

This groups; it does not replace `utilization.log`, which keeps the per-device view.

### 2.6 `latency_cluster.log` — latency distributions

Written alongside the file above, from pooled raw samples shipped by the devices.

```
1785128504874248492 cluster=intermediate_queue_0 role=cloud kind=service n=336 mean_ms=3332.833 p50_ms=3470.922 p95_ms=3966.246 max_ms=4367.941
1785128504874248492 cluster=intermediate_queue_0 kind=e2e n=336 mean_ms=69655.623 p50_ms=69379.207 p95_ms=102639.355 max_ms=111760.937
1785128504874248492 SYSTEM kind=e2e n=... mean_ms=... p50_ms=... p95_ms=... max_ms=...
```

| `kind` | Span | Clock |
|---|---|---|
| `service` | that device's own `get_input → output`, reported per role | one clock — exact |
| `pipeline` | the same unit from ready to published, **including hand-off queue waits**, per role | one clock — exact |
| `e2e` | edge batch start → completing tier's output, one series per cluster | two machines — inherits any clock offset between them |

`service` samples sum to the matching `busy_s` in `utilization_cluster.log`, which makes it the
only latency directly comparable against utilization. `pipeline` adds the in-process queue
waits, so it scales with **queue depth** rather than with device speed — a run at depth 4
measured `service ≈ 11.6 s` against `pipeline ≈ 57.6 s` on the same hardware. When `pipeline ≫
service` the fix is a shorter queue, and throughput does not depend on it. `role=` is absent on
`e2e` and `SYSTEM` lines.

Percentiles are **nearest-rank over the sorted pooled samples** (no interpolation), so every
number printed is a latency that was actually observed. Devices ship raw samples rather than
summaries precisely so the percentiles can be computed over the pool — averaging per-device
percentiles is not a valid operation ([Server.py:578-593](src/Server.py#L578-L593)).

### 2.7 `map_window.log` — mAP pipeline 1 (sliding window)

Written in [Server.py:840-855](src/Server.py#L840-L855). A window of `map.window_batches`
(default 16) consecutive batches slides one batch at a time; each window gets a full mAP.

```
1785128505579200616 cluster=intermediate_queue_0 window=0 batches=0-15 frames=512 mAP50_95=0.0787 mAP50=0.1465
1785128505579200616 cluster=intermediate_queue_0 window=1 batches=1-16 frames=512 mAP50_95=0.0833 mAP50=0.1544
```

This is the **accuracy counterpart of `window_fps`** — the mAP-over-the-run series. Line it up
against `batch_done_ns.log` and `cut_change_ns.log` on a shared ns-epoch axis to see how
accuracy responded to a split-point change. If a run has fewer batches than the window size,
one window covers everything.

### 2.8 `map.log` — mAP summary (both pipelines)

Written in [Server.py:1019-1044](src/Server.py#L1019-L1044). Two lines per cluster plus one
`OVERALL` line per pipeline.

```
1785128505579200616 cluster=intermediate_queue_0 WINDOW mAP50_95=0.1004 mAP50=0.1857 (mean of 14 window(s) x 16 batches, step 1)
1785128505579200616 cluster=intermediate_queue_0 ALL    mAP50_95=0.0882 mAP50=0.1632 (905/905 GT frame(s) matched)
1785128505579200616 OVERALL WINDOW mAP50_95=0.1006 mAP50=0.1859 (avg over 2 cluster(s))
1785128505579200616 OVERALL ALL    mAP50_95=0.0883 mAP50=0.1634 (avg over 2 cluster(s))
```

| Tag | Pipeline | Definition |
|---|---|---|
| `WINDOW` | 1 | mean over all sliding windows. Over-weights frames that sit in more windows — use it for *trend*, not for a headline number |
| `ALL` | 2 | one metric fed every scorable frame of the run, each frame weighted exactly once. The accuracy counterpart of SYSTEM FPS — **use this as the headline number** |

`ALL`'s frame count is capped by ground truth: `map/label/` labels only the first N frames, and
workers stop writing pred files past that, so `905/905` means every labelled frame was scored.

`OVERALL` is always written when at least one cluster scored — even for a single cluster, where
it just repeats that cluster's numbers — so a parser can always find one authoritative line per
pipeline. The console summary omits it in that case.

Ground truth is the server's **own local** `map/label/frame_NNNNNN.txt`
(`class_id cx cy w h`, normalized to 640×640). Predictions come from the workers as
`class_id cx cy w h confidence`. If `map/label/` is missing, mAP is skipped with a warning.

**When mAP is missing entirely:** `map.log`/`map_window.log` stay empty and the console
distinguishes the two causes — *no scorable frames* (ran, found nothing) vs. *no COCO backend*
(`pip install faster-coco-eval`, see [Server.py:33-85](src/Server.py#L33-L85)). They need
completely different fixes.

### 2.9 `cut_change_ns.log` — adaptive split-point changes

Written in [Server.py:1340-1341](src/Server.py#L1340-L1341), only when `adaptive.enable: true`.

```
1785127901123456789 intermediate_queue_0: cut 11->12 deeper
```

`deeper` = the edge now runs more layers (cloud was the bottleneck); `shallower` = the cloud
takes more (cloud was starved).

> **Caveat.** This file is truncated at startup *only when the adaptive controller is enabled*
> ([Server.py:264-266](src/Server.py#L264-L266)). A later non-adaptive run therefore leaves an
> earlier dynamic run's file in place — and `_archive_results` copies any non-empty result file,
> so a `split`-tagged archive can end up carrying a stale `cut_change_ns.log`. Ignore that file
> in any archive whose tag is not `dynamic`.

### 2.10 `free_time.log` — per-device idle time (optional)

One line per device, drained at shutdown like `utilization.log`. **Free time is the wall clock in
which the device did nothing at all** — no input, no compute, no encode/decode, no transfer, no
bookkeeping — computed as the run span minus the **union** of every one of its lanes' busy
intervals.

```
1786024095544125400 client=7e3dd352-8b9a-4d6b-8173-412705d9bbe3 role=edge  machine=machine-2 cluster=intermediate_queue_0 device=cpu  span_s=467.394 busy_s=181.004 free_s=286.390 free=61.28% gaps=57 longest_free_ms=9821.400 host_idle=54.10%
1786024095544125400 client=c6fdabe4-63e1-45f7-ad21-ef4de628bd3b role=cloud machine=machine-7 cluster=intermediate_queue_0 device=cuda span_s=599.152 busy_s=571.930 free_s= 27.222 free= 4.54% gaps=170 longest_free_ms=812.100 host_idle=11.30%
```

| Field | Meaning |
|---|---|
| col 1 | ns-epoch **arrival** of the report (not a device timestamp) |
| `machine` | the host this device process runs on — several devices may share one |
| `span_s` / `busy_s` / `free_s` | `busy_s` is the **merged** intervals; `busy_s + free_s == span_s` exactly |
| `free` | `free_s / span_s`, ≤ 100% |
| `gaps` / `longest_free_ms` | how many separate idle stretches, and the longest |
| `host_idle` | the OS's own idle share for the whole machine, across all processes. Optional |

> **`free` and `utilization` (§2.4) are not complements.** Utilization is `busy/total` over one
> lane's `get input → output` window; free time is every lane over the whole run. A wait *inside*
> the unit window counts as busy for one and free for the other, and work on a second lane counts
> for the other and not at all for the first. **`free% + utilization% ≠ 100%`, and neither can be
> derived from the other.** A device at 40% utilization and 3% free is not idle — it is doing work
> the unit window never saw, and that gap is the finding.

> **Values may be right-aligned.** `free_s= 27.222` has a space after the `=`, which the universal
> grammar in §1 does not allow. Readers here tolerate it; a parser that does not silently drops
> every padded key and charts only the devices whose numbers happened to be wide.

### 2.11 `free_time_cluster.log` — free time rolled up (optional)

Six line kinds in one file. They are told apart by their **flag** before their keys: a `FREE`
line also carries `cluster=`, so reading the scope first files every breakdown as a cluster total.

```
1786024095544125400 cluster=intermediate_queue_0 ALL devices=8 free=48.21% free_mean=46.02% free_s=2055.900 span_s=4264.621
1786024095544125400 cluster=intermediate_queue_0 role=edge devices=6 free=61.28% free_mean=60.11% free_s=1719.200 span_s=2805.700
1786024095544125400 SYSTEM FREE reason=input free_s=1802.400 share=87.67%
1786024095544125400 SYSTEM KIND kind=inference busy_s=1204.700 share=28.25%
1786024095544125400 MACHINE machine=machine-2 devices=3 free=12.40% free_s=57.900 span_s=467.394 merge_slop_s=0.000 host_idle=54.10%
1786024095544125400 SYSTEM devices=12 clusters=2 machines=12 free=39.55% free_mean=38.10% free_s=2643.700 span_s=6684.796
```

| Line kind | Marked by | Covers |
|---|---|---|
| cluster total | `cluster=` + `ALL` | every device in that cluster |
| cluster × role | `cluster=` + `role=` | devices of one role in one cluster |
| free breakdown | `FREE` + `reason=` | why that scope was free |
| busy breakdown | `KIND` + `kind=` | where that scope's busy time went |
| machine | `MACHINE` + `machine=` | every device **process** on one host |
| system | `SYSTEM` | every device |

- `free` is **pooled** (`Σfree / Σspan`); `free_mean` is the plain mean of per-device percentages,
  on `ALL`/`SYSTEM` only. The same pair, and the same reading, as §2.5.
- **`FREE reason=` shares sum to exactly 100%** of that scope's free time. Overlapping reasons are
  attributed in a fixed priority so nothing is double counted, and whatever no reason covers is
  reported as `unaccounted` rather than dropped.
- **`KIND kind=` shares may sum to more than 100%.** Per-kind sums overlap across lanes by
  construction; only the merged `busy_s` in §2.10 is exclusive. This is correct output, not a bug.
- `MACHINE` lines come from the **union of the busy intervals** of the processes on that host,
  never from their ratios: two processes each 50% free can keep a machine 100% busy by
  interleaving. Intervals are never unioned across machines — that is the one place device
  timestamps are compared, and it is valid only because processes on one host share a clock.
- `merge_slop_s` is non-busy time swallowed by capping the shipped interval list. It biases the
  answer toward *less* free time, which is the safe direction, and states its own error bar.
- A machine running no devices (the controller's own host) may appear with `devices=0` and only
  `host_idle`. It is still part of the fleet.

`free` against `host_idle` is a four-way read: both high is spare capacity; free-high with
idle-low means something else on the box is eating the CPU; free-low with idle-high means the
pipeline is blocked on I/O rather than compute; both low is saturated.

### 2.12 `free_time_series.log` — free time over the run (optional)

One line per device per fixed-width bucket. Written at shutdown, but describes the whole run.

```
1786024095544125400 client=7e3dd352 role=edge machine=machine-2 cluster=intermediate_queue_0 i=0 t_offset_s=0.000 bucket_s=1.000 free=12.40%
```

- The leading timestamp is the report's server-clock **arrival**, exactly as in §2.4. The position
  in the run is carried by `t_offset_s`, which is on the **device's own** clock.
- **Do not conflate them.** Devices start at different moments, so two rows with the same offset
  are not the same instant, and this series cannot be cut on a window of the system's clock.
- `bucket_s` is on every line rather than assumed, so a long run may widen its buckets without
  breaking a reader.

### 2.13 `broker_ram_ns.log` — the queue host's RAM, sampled (optional)

The one measurement not reported by a process we wrote. The machine hosting the message queue runs
only third-party infrastructure, is on the critical path anyway, and when it is the bottleneck
**every symptom shows up somewhere else**: a broker at its high-water mark does not fail, it blocks
publishers, and on the worker that looks like a stall with no local cause. *The next stage is slow*
and *the broker stopped accepting* produce almost identical worker-side telemetry — this file is
what separates them. The server samples it over one long-lived SSH session, live-appended so a run
that dies still leaves the series behind.

```
1786282738811691751 host=192.168.101.91 source=ssh total_mb=5921.5 used_mb=1586.2 used=26.79% avail_mb=4335.3 free_mb=3770.5 cached_mb=747.0 swap_used_mb=1032.3 rabbit_rss_mb=87.8
```

- `used_mb` is **`MemTotal − MemAvailable`**. `MemTotal − MemFree` counts reclaimable page cache as
  used and reads ~90% on any machine that has touched a disk — always alarming, never actionable.
- **`source=` is part of the number.** `ssh` is host memory across every process on the box;
  `rabbitmq_api` is the management-API fallback, where `used_mb` is the **broker process**, not the
  host. They answer different questions and are never silently substituted: an unlabelled fallback
  produces a plausible number meaning something other than what the file name says.
- `rabbit_rss_mb` answers *is it full because of the thing I care about*, which has a different fix
  from *is the box full*. `swap_used_mb` matters because a host that is swapping is already past
  the point where its latency contribution is stable.
- Sampling starts where work is dispatched, so the first sample is the baseline with every queue
  empty, and stops after the shutdown drain — a curve that does **not** fall there is the signal
  that something is still holding units.

### 2.14 `broker_ram.log` — the RAM summary (optional)

Four flagged lines at shutdown.

```
1786282740000000000 BROKER host=192.168.101.91 source=ssh samples=1187 interval_s=1.000 span_s=1186.4 total_mb=5921.5 t_start_ns=… t_end_ns=…
1786282740000000000 USED   min_mb=… mean_mb=… p50_mb=… p95_mb=… max_mb=… min=…% mean=…% p95=…% max=…%
1786282740000000000 DELTA  start_mb=… end_mb=… growth_mb=… peak_over_start_mb=…
1786282740000000000 RABBIT mean_rss_mb=… max_rss_mb=… swap_max_mb=…
```

Percentiles are **nearest-rank over the raw samples**, no interpolation — the same rule as §2.6, so
every number printed is a value that was actually observed.

Read it in this order: `DELTA growth_mb` (did the run leak? positive growth across a run that
drained completely means units are still buffered somewhere) → `DELTA peak_over_start_mb` (the real
headroom question — compare against `unit_size × max_queue_depth`) → `USED max` against `total_mb`
(how close to the wall) → `RABBIT max_rss_mb` (the broker, or something else on the box) →
`swap_max_mb` (non-zero invalidates any latency conclusion drawn from the same run).

> **`samples=0` is a result.** When sampling never happened the `BROKER` line is still written,
> with the reason in trailing parentheses: `samples=0 (permission denied)`. A missing file is
> indistinguishable from a run where the host was fine; that line is not, and it is surfaced as a
> report warning rather than as a chart that quietly did not draw.

### 2.15 `message_size.log` — what one worker puts on the wire (optional)

Every other file here measures *time*. This one measures **bytes**: the size of the payload a
worker hands to the transport, taken on the worker, once per published message.

It is what makes three of the others readable. Utilization (§2.4) says a worker was busy; this
says whether it was busy computing or busy shipping. The queue host's memory curve (§2.13) shows
the queue filling; message size × queue depth says whether that is the payload or something else.
And a `send`-dominated free-time profile (§2.11) means nothing until you know how many bytes each
send moved.

```
1786366279200770600 client=machine-2 role=edge machine=machine-2 cluster=intermediate_queue_0 mode=split splits=5 compress=on num_bit=8 batch_size=32 n=504 total_mb=19657.464 mean_mb=39.003 p50_mb=39.022 p95_mb=39.613 max_mb=40.098 min_mb=37.909 span_s=714.260 rate_mb_s=27.521 per_frame_mb=1.2188
```

| Key | Meaning |
|---|---|
| `n` | messages this worker published |
| `total_mb` | bytes it put on the wire over the whole run |
| `mean_mb`, `p50_mb`, `p95_mb`, `max_mb`, `min_mb` | per-message size. Percentiles **nearest-rank over the raw samples**, no interpolation — same rule as §2.6 |
| `span_s` | first publish → last publish, on that worker's clock |
| `rate_mb_s` | `total_mb / span_s` — this worker's egress, to compare against its share of the link |
| `per_frame_mb` | `mean_mb / batch_size`, so runs with different batch sizes compare |
| context keys | `mode`, `splits`, `compress`, `num_bit`, `batch_size` — whatever determines the size |

**Exactly one worker measures**, and the server picks it: the first worker that registered at the
first stage. Registration order needs no configuration, is stable within a run, and is already
known before any work is dispatched; the first stage is the one whose output crosses the network,
and every worker in a group publishes the same payload shape from the same split point — so nine
measuring produce one number nine times at nine times the cost. **The worker never decides this
for itself**: the flag travels in the dispatch message, so the job cannot land on two machines or
on none.

Two details the number depends on:

- **The size is recorded before the publish call**, not after. Both orderings look equivalent
  until the transport blocks — and a broker at its high-water mark, or a saturated link stalling
  mid-write, are exactly the runs this exists to explain. Measured after, the sample for the
  message that stalled is written late, or never.
- **Serialized bytes** — what the transport is handed, after your own compression and framing,
  before the transport's own. A pre-serialization tensor size is a different quantity.

**The context keys are not optional decoration.** The same worker at the same split point with
compression off is a different number entirely, so a size without them is unreproducible.

Size keys use **MB = 10⁶ bytes**, matching §2.13, so a payload size and the host's memory growth
compare without a conversion in between.

### 2.16 `message_size_series.log` — message size over the run (optional)

One line per published message — the plottable series behind the summary above.

```
1786366279200770600 client=machine-2 cluster=intermediate_queue_0 i=0 t_offset_s=0.000 batch_id=0 bytes=38897647 mb=38.898
```

| Key | Meaning |
|---|---|
| col 1 | ns-epoch arrival of the report at the **server**, identical on every line of a report |
| `i` | sample index |
| `t_offset_s` | seconds since **that worker's own first publish** |
| `bytes` | exact integer — the authoritative value |
| `mb` | the same number in MB, for readers that plot without converting |

- Both columns on purpose: `bytes` is exact and `mb` keeps the file readable, and a reader that
  rounds its own MB from `bytes` still agrees with the summary. **Read `bytes`.**
- Sample times are **offsets**, never absolute device timestamps: the server writes this into a
  shared result file, and every absolute timestamp in a shared file is the server's own clock.
  Offsets are computed within one device, so they are exact and locate a sample in the run without
  ever being compared against another machine's clock — and, like §2.12, that is why a window on
  the system's clock leaves this series whole.
- A long run decimates the shipped series **evenly** rather than truncating, so it still spans the
  whole run. The summary statistics are computed over the **full** sample set: decimation may
  coarsen a plot, never a number.

**Reading it.** `mean_mb` × the queue depth cap is the RAM the queue host must hold — compare
against `DELTA peak_over_start_mb` in §2.14; when the host's peak is far larger, something is
buffering you did not account for. `rate_mb_s` × the workers sharing the link is offered load,
the first check on whether the network or the compute is the bottleneck. `max_mb` against the
transport's message-size limit is usually why a run at a deeper split point died. A wide
`p95 / p50` means the payload varies with content, which makes any single-number bandwidth
estimate optimistic. And the series against `batch_done_ns.log`: size flat while throughput falls
is a transport problem, size climbing is a workload one.

---

## 3. `results/` — the run archive

`_archive_results` ([Server.py:1063-1116](src/Server.py#L1063-L1116)) runs once, after the last
shutdown pipeline has written its files, so the archive is a complete snapshot.

```
results/results_<MMDD>_<HHMM>_<tag>/
├── batch_done_ns.log
├── fps_cluster.log
├── fps_cluster_ns.log
├── latency_cluster.log
├── map.log
├── map_window.log
├── utilization.log
├── utilization_cluster.log
├── cut_change_ns.log        (adaptive runs only)
└── config.yaml              (the config that produced these numbers)
```

**Tag** — which configuration produced the run:

| Tag | Condition |
|---|---|
| `only_cloud` / `only_edge` | `experiment.enable: true` with that mode |
| `dynamic` | split mode **and** `adaptive.enable: true` |
| `split` | split mode with a fixed cut |

**Rules**

- **Copies, not moves** — `log-path` keeps its own copies where every existing reader expects
  them; the next run truncates them itself.
- **Empty files are skipped**, so a zero-length log is never archived as a misleading result.
- **Collision-safe** — two runs finishing in the same minute get `…-2`, `…-3`, and so on.
- `config.yaml` is copied in, so the archive reads on its own months later without guessing the
  cut / batch-size / clustering settings.
- Failure is non-fatal: a filesystem problem prints `[Archive] failed: …` and the run still
  closes the broker connection and exits cleanly.
- If everything was missing or empty you get `[Archive] WARNING: every result log was missing
  or empty` — a run that produced nothing.

Existing archives live in [results/July27th/](results/July27th/) (`split/`, `dynamic/`, plus
`imgs/` with the rendered plots) and the notebooks in [results/visual/](results/visual/).

---

## 4. `map/pred_collected/` — collected prediction files

Built by `_collect_map_pred` ([Server.py:958-989](src/Server.py#L958-L989)): each tier that ran
`postprocess_yolo` zips its own `map/pred/*.txt` and publishes it to `map_pred_queue` tagged
with its cluster id. The server unpacks per cluster:

```
map/pred_collected/
├── intermediate_queue_0/frame_000001.txt …
└── intermediate_queue_1/frame_000001.txt …
```

- **Write-once**: a frame index already unpacked for a cluster is kept, never overwritten, so a
  second edge in the same cluster reprocessing the same video can't clobber it.
- Wiped at startup *and* rebuilt at every shutdown collection — it is the server's own scratch
  space, not a result you need to keep. The numbers derived from it live in `map.log`.
- Which tier is expected to publish depends on mode: `only_edge` → the edges, otherwise → the
  cloud tier.

---

## 5. Files the server *deletes*, and files it does *not* write

Cleared once at startup in [Server.py:94-115](src/Server.py#L94-L115) — this happens in the
Server (which starts once) rather than per-Scheduler, because doing it per-client made
later-starting clients wipe files earlier clients were already writing:

| Deleted at startup | Why |
|---|---|
| `metrics_raw_*.csv`, `metrics_pivoted_*.csv`, `metrics_pivot_*.lock` | client-written per-batch metrics from the previous run |
| `detections_stream.jsonl` | streamed detections from the previous run |
| `map/pred/`, `map/pred_collected/` | `map/pred` is write-once per frame, so leftovers would "win" forever and silently poison every future run's mAP |

These are produced by **[src/Scheduler.py](src/Scheduler.py) on the client devices**, not by the
server, and are **not** part of the `results/` archive:

| File | Written by | Content |
|---|---|---|
| `metrics_raw_<queue>_<client>.csv` | each device | per-batch raw metrics |
| `metrics_pivoted_<queue>.csv` | one device per cluster (lock-elected) | joined edge+cloud view: `batch_id, batch_size, best_cut, edge_device, edge_latency_ms, edge_ram_mb, edge_message_size_bytes, cloud_device, cloud_latency_ms, cloud_ram_mb, e2e_latency_ms` |
| `detections_stream.jsonl` | the postprocessing tier | one line per frame: `{"frame": 1, "dets": [{"box": […], "score": …, "class": …}]}` — flat RAM over long videos |
| `detections.json` | the postprocessing tier, at the end | the stream rebuilt as `{frame: dets}`; gated by `detections.save_json` |
| `map/pred/frame_NNNNNN.txt` | the postprocessing tier | `class_id cx cy w h confidence`, 640-normalized |

> Note: `detections.json` is **not** cleared at startup (only the `.jsonl` stream is), so an old
> one survives until the next run rebuilds it. Check its mtime before trusting it.

---

## 6. Console-only output

Not saved anywhere — capture the terminal if you need it:

- The framed `[SYSTEM FPS]` block: whole-run fps, steady-state fps, and the `[ref mean, N/U]`
  arithmetic mean of `1/dt` (biased high, kept for comparison only — do not use it), plus the
  batch count and stop reason ([Server.py:496-533](src/Server.py#L496-L533)).
- The `[PER-CLUSTER UTILIZATION & LATENCY]` block — same data as the two cluster logs, formatted.
- The `[mAP PIPELINE 1/2]` summary block ([Server.py:881-921](src/Server.py#L881-L921)).
- Hungarian clustering result (`print_result`), per-device profiling and bandwidth lines, and
  live `[FPS] DONE #n window_fps=…` progress.

---

## 7. Quick recipes

| Question | File | How |
|---|---|---|
| Headline throughput | `fps_cluster.log` | the `SYSTEM` line's `fps` |
| Did the clusters balance? | `fps_cluster.log` | compare `share=` across clusters |
| Throughput over time | `batch_done_ns.log` | plot col 2 against col 1 |
| Per-cluster throughput over time | `fps_cluster_ns.log` | group by `cluster=`, plot `window_fps` |
| Is the cut in the right place? | `utilization_cluster.log` | compare `role=edge` vs `role=cloud` utilization |
| Tail latency | `latency_cluster.log` | `kind=e2e`, `p95_ms` |
| One straggler device? | `utilization.log` | the per-device `utilization=` column |
| Headline accuracy | `map.log` | the `OVERALL ALL` line |
| Did accuracy move with the cut? | `map_window.log` + `cut_change_ns.log` | shared ns-epoch x-axis |
| What config produced this? | `results/…/config.yaml` | archived alongside the numbers |

Related specs: [fps_measure_guide.md](fps_measure_guide.md),
[utilization_guide.md](utilization_guide.md), [visual_guide.md](visual_guide.md).
