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
| **Control** | The real machines: connect over SSH, run commands, start a run |

## Simulate vs Live

A toggle in the header.

- **Simulate** — maths in your browser. No devices, no setup. Good for "what if
  I cut at layer 6 instead of 9?"
- **Live** — talks to real machines over SSH and RabbitMQ, and streams real
  numbers back.

Simulate works out of the box. Live needs machines to talk to.

## Reports

When a run finishes, point the app at the directory it wrote. You get a set of
charts and a box beside each one for the note explaining what it showed.

Reports are just folders under `backend/reports/`:

```
backend/reports/3007-1803_split/
  manifest.json      what the run was
  imgs/*.png         the charts
  logs/              the raw numbers they were drawn from
```

Nothing is in a database, so copying a folder to another machine is the whole
export story. Drop it in and it appears in the list.

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
