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

A terminal with a fleet attached. Work down the page.

### 1 · Control server — the machine you log into first

Fill in the address, SSH port, user and password, then press **Connect (SSH)**.
The dot beside the card title goes green when the login works.

- **Reach devices through this server** — tick this when your edge and cloud
  boxes sit on a private network. Every device is then dialled *through* this
  machine, so `10.0.1.x` addresses your laptop cannot reach still work.
- **Broker (RabbitMQ) · optional** — the folded strip underneath. You only need
  it to actually run split inference. *Check broker + control API too* tests all
  three legs and reports each one: `ssh ✓ · amqp ✓ · api ✓`.
- Once connected, collapse the card with the caret at its top right. It is
  settings you set once, and it pushes the target list off screen.

### 2 · Targets — who the next command goes to

Devices are grouped by stage, and each group has its own **select all**. The
header counts what is ticked. The control server is a group of its own at the
top, so you can aim at it exactly like a device.

The ⚙ on a row opens that device's login form, with **copy login** / **paste**
so the second machine is not retyped.

### 3 · The command line

A `$` box with a working-directory box in front of it. Every command gets a fresh
shell, so `cd` on its own cannot stick — set the directory here instead. The chips
underneath are your saved commands: click one to load it, **edit** to change the
list (and the directory shortcuts). Then **▶ Run**.

| If you want to | Do this |
|---|---|
| Start something long (`python3 src/Server.py`) | Just run it. The call returns straight away and the output keeps streaming |
| Stop it | Type `^C` in the command box and press Run |
| Reboot, or restart an agent | Run it — you get a confirmation prompt first |
| Run something not on the allow-list | It is refused, and the message lists what is allowed |

### 4 · Files — push to many, pull from one

The top row sends a file to every ticked device. The bottom row works on a single
device: **browse** lists a directory (click a folder to go deeper, a file to fill
the box), **⇩ Pull** downloads it to your machine.

### 5 · Consoles

The big console is a fan-out view of every target at once. The strip beside it is
one small console per machine — click one to promote it to the big view, **clear**
to empty it.

### Two buttons in the header belong to this work

- **⟳ measure all** — SSH into every device and replace its guessed specs
  (GFLOPS, MB/s, latency) with timed ones. Each stage has its own **⟳ measure**.
- **⇧ Deploy** — push the model shards and agents to every cluster. Deploy first,
  then run.

## The Visual tab — what the run actually did

A run leaves a directory of plain-text logs on the device. This tab turns that
directory into charts.

1. **Point at it.** The path box takes a directory on the device; **browse** walks
   the filesystem if you do not remember it.
2. **Name it.** *Case test* is how you will recognise this run in a week —
   `cut6-8bit` beats `run7`.
3. **Press Analyze.** The backend reads the logs, picks a chart form per metric
   (a reading over time becomes a trend; a single number becomes a tile rather
   than a one-bar bar chart), draws them, and sends the images back.

What comes back:

| | |
|---|---|
| **Tiles** | The numbers that are the story, across the top. The change is written in words, not just colour |
| **Gallery** | Every chart, numbered and tagged by kind — trend, comparison, distribution, delta, breakdown. Click one to open it full size |
| **⚙ on a chart** | Hide a series, rename the title or either axis, then **Apply** — the backend redraws it. **Reset** puts it back. The gear takes on colour once a chart differs from the default |
| **Note box** | Under every chart, for what that chart showed |
| **Review** | One line at the bottom, for the run as a whole |

**⇩ Save report** writes the notes and review beside the images, so the reading
outlives the browser tab.

**History** browses what you saved: pick a day, then a run from that day. The ✎
marks a run that was actually reviewed; ✕ deletes one.

**Compare** pins up to three reports side by side. Charts line up row by row and
tiles are matched by label, so "did the new cut help?" is one screen. ← and →
step through the charts, Esc returns to the full stack.

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
