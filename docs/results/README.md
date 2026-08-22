# Results gallery

Rendered figures from the split-inference fleet — 13 machines (one control server,
9 edge workers, 3 cloud workers) running six projects against the same video.

Each folder below is one **case**: a set of figures that answer one question,
plus a README explaining what was measured, what the figures show, and what is
not safe to conclude from them.

| Case | Network | What it answers |
|---|---|---|
| [static-all-projects](static-all-projects/) | unshaped | How do the six projects compare on a clean network? |
| [static-split-detail](static-split-detail/) | unshaped | Inside one Split run: where does throughput go, and what does it cost on the wire? |
| [static-accuracy-map](static-accuracy-map/) | unshaped | Split vs DMSF on detection accuracy over the same 905 labelled frames |
| [static-window-20-60](static-window-20-60/) | unshaped | How much of each headline number is ramp-up and drain rather than steady state? |
| [dynamic-all-projects](dynamic-all-projects/) | shaped | The same comparison under a bandwidth-varying network |
| [dynamic-adaptive-tuned](dynamic-adaptive-tuned/) | shaped | Does an adaptive cut-point controller help when bandwidth is the constraint? |

## Reading these

**Images only.** The raw result folders that produced them are deliberately not
in this repository: each run archives its `config.yaml`, which carries plaintext
RabbitMQ and SSH credentials for the lab fleet. The figures and the numbers
quoted in each README are the shareable part.

**Static and dynamic are separate experiments, not two halves of one.** The
static runs were taken across several days on an unshaped network; the dynamic
runs were taken in one overnight schedule under a bandwidth shaper. Comparing a
number from one against a number from the other measures the network change plus
whatever else drifted in between. Compare within a case.

**Absent data is drawn as absent.** Where a metric was never measured, the
figures say "not measured" rather than plotting a zero — a missing broker-RAM
sampler and a broker that used no memory are different facts and the charts keep
them different.

## The network the dynamic cases ran under

A traffic shaper on the broker host, applied to both AMQP (5672) and the
management API (15672), in both directions:

```
distribution : poisson,  mean 60 Mbit/s,  std 20,  clamped to [10, 80]
hold         : each rate held 60 s
seed         : 42          # deterministic — every run replays the same sequence
```

The seed is what makes the dynamic cases comparable to each other: two runs
started at the same point in the profile see the same bandwidth, minute for
minute. It is also why the dynamic figures should not be read against the static
ones as if only one thing had changed.
