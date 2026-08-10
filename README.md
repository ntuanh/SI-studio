# SI-studio

Distributed AI inference — simulate & visualize.

Split a neural network across edge and cloud machines, watch it run, and chart
what happened.

## What it does

You have a model that is too heavy for a small edge device. SI-studio cuts it in
two: the edge runs the first layers, sends the intermediate result over
RabbitMQ, and the cloud runs the rest. The interface lets you pick where to cut,
run it, and see what that choice cost you in speed and accuracy.

## Try it in 10 seconds

Double-click `split-inference-pipeline.html`. That is the whole interface in one
file — no install, no server. Everything below is optional.

## Run it properly

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python tools/build_web.py
uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>.

`build_web.py` is not optional — it turns the HTML file above into the website.
Skip it and the page tells you so.

## The tabs

| Tab | What it is for |
|---|---|
| **Stages** | Your model's layers, and how heavy each one is |
| **Pipeline** | Where the cut goes, and what crosses the network |
| **Config** | Batch size, quantization, and the rest of the knobs |
| **Clusters** | Which machines are edge, which are cloud |
| **Simulation** | Predicted speed and latency, computed in the browser |
| **Control** | The real machines: log in, run commands, move files |
| **Visual** | Charts from a run that already finished |

The first five decide *what* to run. The last two run it and read the result —
those are the two walked through below.

## Simulate vs Live

A toggle in the header.

- **Simulate** — maths in your browser. No devices, no setup. Good for "what if
  I cut at layer 6 instead of 9?"
- **Live** — talks to real machines over SSH and RabbitMQ, and streams real
  numbers back.

Simulate works out of the box. Live needs machines to talk to.

The **Control** tab is the exception: it always talks to real machines, whichever
way the toggle is set. The toggle only decides where the *numbers* come from.

## The Control tab — the whole fleet on one screen

![The Control tab: the Control server card and the Targets list down the left,
the command line, the file transfer card and the consoles down the
right](docs/img/ui-control.png)

Left column is *who*, right column is *what*. Read it card by card.

**Control server** — the machine you log into first. Address, SSH port, user,
password, then **Connect (SSH)**; the dot by the title goes green when the login
works.

- **Reach devices through this server** — tick it when your edge and cloud boxes
  are on a private network. Every device is then dialled *through* this machine,
  so the `10.0.1.x` addresses your laptop cannot route to still work. (That is
  what the `jump host` tag under the first target means.)
- **Broker (RabbitMQ) · optional** — the folded strip at the bottom. Only needed
  to actually run split inference. Its *check broker + control API too* button
  tests all three legs and reports each: `ssh ✓ · amqp ✓ · api ✓`.
- Collapse the card once it is connected — settings you set once should not push
  the target list off screen.

**Targets** — who the next command goes to. Devices are grouped by stage, each
group has its own **select all**, and the header counts what is ticked. The
control server is a group of its own at the top, so you can aim at it exactly
like a device. The ⚙ on a row opens that device's login form, with **copy login**
/ **paste** so the second machine is not retyped.

**Run command on N device(s)** — the chips are your saved commands (**edit**
changes the list), the **directory** row is where they run, and the `$` box is
free-form. **Ctrl+Enter** (⌘+Enter on a Mac) in that box is **▶ Run**, so a
command you are still getting right does not send you back to the mouse between
attempts. `cd` on its own cannot stick, because every command gets a fresh
shell — that is what the directory row is for.

| If you want to | Do this |
|---|---|
| Start something long (`python3 src/Server.py`) | Just run it. The call returns at once and the output keeps streaming |
| Stop it | Type `^C` in the command box and press **▶ Run** |
| Reboot, or restart an agent | Run it — you get a confirmation prompt first |
| Run something off the allow-list | It is refused, and the message lists what is allowed |

**Files** — the top row pushes one file to every ticked device; the bottom row
works on a single one. **browse** lists a directory (click a folder to go deeper,
a file to fill the box), **⇩ Pull** downloads it to your machine.

**Consoles** — the big one is every target at once. The strip on the right is one
small console per machine; click one to promote it to the big view.

Two header buttons belong to this work: **⟳ measure all** SSHs into every device
and replaces its guessed specs with timed ones, and **⇧ Deploy** pushes the model
shards and agents to every cluster. Deploy first, then run.

## The Visual tab — what the run actually did

A run leaves a directory of plain-text logs on a device. This tab turns that
directory into charts.

![The Visual tab: the path and case-test row, the History bar, and the row of
headline tiles](docs/img/ui-visual.png)

1. **Point at the directory** — or **browse** to it if you do not remember it.
2. **Name it.** *Case test* is how you will recognise this run in a week:
   `cut6-8bit` beats `run7`.
3. **Analyze.** The backend reads the logs and picks a chart form per metric — a
   reading over time becomes a trend, a single number becomes a tile rather than
   a one-bar bar chart — draws them, and sends the images back.

The tiles across the top are the numbers that *are* the story. Everything below
them is the gallery, one card per chart.

**What the gallery holds.** Cards 01–10 are the run's story — throughput,
latency, utilization, accuracy — and are drawn for every run. Cards 11–17 are
diagnostics, drawn only when the run measured what they need:

| Card | Answers | Needs |
|---|---|---|
| 11 Queue wait | Is the hand-off queue costing more than the devices? | `kind=pipeline` latency |
| 12 Free time and utilization | Which devices were idle — and where do the two measures disagree? | `free_time.log` |
| 13 Free by reason, busy by kind | Was it starvation, congestion, or overhead? | `free_time_cluster.log` |
| 14 Idle time by machine | Is idleness concentrated on particular hosts? | `free_time_cluster.log` |
| 15 Free time over the run | *When* was each device idle? | `free_time_series.log` |
| 16 Queue-host RAM over the run | Did the broker back up while throughput fell? | `broker_ram_ns.log` |
| 17 Queue-host RAM profile | How close to full, and what did the run leave behind? | `broker_ram.log` |

Free time and queue-host RAM are optional, all-or-nothing features: a run either
writes all of a group's files or none, so you get a whole group of cards or no
card from it. Numbering is fixed, so a run that measured neither still has 07
meaning utilization — the gap is left rather than closed.

Two of these are easy to misread, so the cards say it outright. **Free time is
not `1 − utilization`**: utilization is `busy / total` over one lane's unit
window, free time is the whole run minus every lane's busy intervals merged, so
a wait inside the unit window counts as busy for one and free for the other.
They do not sum to 100%, and where they disagree loudly the gap *is* the
finding. And **the queue host's RAM carries its source**: `ssh` is host memory
across every process on the box, `rabbitmq_api` is the broker process alone. A
figure without that label is a plausible number meaning something other than
what it says.

### Charting part of a run

The first batches of a run are warm-up and the last are the tail draining. Both
drag the averages around and put a ramp on either end of every timeline, which
is the shape of the harness rather than of the split.

**⧗ Whole run** is the button that changes that. Click it, type `5` and `95`,
and the report describes the middle 90% of the run instead of all of it.

The two numbers are percentages of **the system's own clock** — first batch
completed to last batch completed. On a 1,340-second run, `5`–`95` is the 1,206
seconds between 67 s and 1,273 s in. That one span cuts every chart. It is
deliberately not a per-series slice: trimming each cluster at its own 5% mark
would give the clusters different stretches of the run, and then comparing them
compares two different experiments.

**The numbers are recomputed, not relabelled.** Throughput is counted again
from the batch completions inside the span — 454 batches over 1,206 s is that
span's throughput, by the same arithmetic the run used for the whole. (The
check that it *is* the same arithmetic: run the window across the entire clock
and it reproduces the run's own `steady_fps` exactly.) `steady_fps` itself
disappears from a windowed report, because a window that excluded the warm-up
has already done that job, and a second bar meaning the same thing is a
comparison that is not in the data.

**Some things cannot be recomputed, and every chart says which side it is on:**

| Stays whole-run | Because the run wrote only |
|---|---|
| Utilization, per device and per cluster | a cumulative `busy_s` / `total_s` |
| Service, pipeline and end-to-end latency | finished `n` / `mean` / `p50` / `p95` / `max` |
| Accuracy over all frames | the finished per-frame match |
| Free time, per device, machine and reason | a merged total per device |
| The queue host's RAM summary | min / mean / p50 / p95 / max over the samples |

There is no per-batch series behind any of them, so nothing here can re-add
them over a slice. Those charts and the stat tiles above the gallery are
labelled **whole run**; the narrowed ones name their window. Both directions
are marked — if only the windowed ones were, an unmarked chart would read as
windowed too.

Free time *over the run* is the one series that stays whole-run anyway. Its
buckets are stamped on each device's own clock rather than the server's, and
devices do not start together — so two buckets with the same offset are not the
same moment, and there is nothing for a span of the system's clock to be
measured against. That card says so, rather than saying "whole run" and leaving
you to wonder which of the two reasons applies. The queue host's RAM *series*
is sampled by the server, so it is cut like any other series.

One consequence worth knowing: mAP is only scored over frames that have ground
truth, which can be a short stretch near the start. If your window holds none
of those scoring windows, the accuracy charts are not drawn and the report says
so rather than going quiet.

The window is *static*: you set it before analysing, it is stored with the
report, and re-drawing a chart later uses it again. Two analyses of one
directory with different windows are two reports, and History marks the
narrowed ones with `⧗5–95%`.

### A chart card

![One chart card with its settings panel open: series pills, title and axis
boxes, Apply and Reset, the chart, and the note box](docs/img/ui-visual-chart.png)

The number (`01`) is the catalogue position and is stable across re-runs. The
chip says what kind of chart it is — trend, comparison, distribution, delta,
breakdown. Click the image to open it full size.

**⚙** opens the settings panel: switch a series off, rename the title or either
axis, then **Apply** and the backend redraws the PNG. **Reset** puts it back to
the guide's default. The gear takes on colour once a chart has been changed, so
you can see which ones you touched without opening them.

The box at the foot of the card is that chart's note. There is a **Review** line
for the whole run at the bottom of the tab, and **⇩ Save report** writes both
beside the images — so the reading outlives the browser tab.

**History** browses what you saved: pick a day, then a run from that day. A ✎
marks a run that was actually reviewed; ✕ deletes one.

### Comparing two runs

![Compare mode: two reports pinned side by side, headline numbers matched by
label, and the same chart drawn from each run](docs/img/ui-visual-compare.png)

**⧉ Compare runs**, then click runs in History to pin up to three. Tiles are
matched by label and charts line up by catalogue number, so "did the new cut
help?" is one screen. **▦ all** stacks every chart; a chip picks one; ← and →
step through them and Esc returns to the full stack.

## Where reports live

Reports are just folders under `backend/reports/`:

```
backend/reports/3007-1803_split/
  manifest.json      what the run was
  imgs/*.png         the charts
  logs/              the raw numbers they were drawn from
```

Nothing is in a database, so copying a folder to another machine is the whole
export story. Drop it in and it appears in History.

`logs/` is what makes ⚙ **Apply** work — a report kept without its logs can still
be read, but its charts cannot be redrawn.

> **Before sharing a report:** `logs/config.yaml` contains your broker password
> in plain text. Redact it first.
>
> ```bash
> sed -i 's/^\(  password: \).*$/\1"REDACTED"/' backend/reports/*/logs/config.yaml
> ```

## Where to go next

| | |
|---|---|
| [`guides/deploy.md`](guides/deploy.md) | Putting this on another machine |
| [`backend/README.md`](backend/README.md) | The API, the SSH layer, running real inference |
| [`guides/visual_guide.md`](guides/visual_guide.md) | How the charts are drawn |
