# Deploying the UI and saved reports to another machine

Target: a Linux box that should serve the interface and let you read past
reports. No RabbitMQ, no SSH to devices, no live runs — see "Full deployment"
at the end for what changes if you want those.

---

## What you are actually moving

Two things, and they behave differently:

| | Lives at | Nature |
|---|---|---|
| **UI** | `split-inference-pipeline.html` (repo root) | A self-extracting bundle — the *source*. `backend/web/` is generated from it and is **not** in git |
| **Reports** | `backend/reports/<id>/` | Plain folders: `manifest.json` + `imgs/*.png` + `logs/`. No database |

`store.listing()` builds the report list by iterating the reports directory and
reading each `manifest.json`. Copy a folder in and it appears — there is no
import step and nothing to migrate.

The consequence of row one: **cloning does not give you a working UI.** You must
run `tools/build_web.py` on the target. Without it `/` returns a 503 page that
says so.

---

## Prerequisites on the target

- Python **3.11+** (the Docker image uses 3.12)
- `git`
- A GitHub Personal Access Token, since the repo is private
  (Settings → Developer settings → Tokens → Fine-grained, scope `Contents: Read`)

```bash
sudo apt update && sudo apt install -y git python3-venv python3-pip
```

---

## 1. Clone

```bash
cd ~
git clone https://github.com/ntuanh/SI-studio.git
# username: ntuanh
# password: paste the PAT, not your account password
cd SI-studio
```

Check the reports arrived:

```bash
ls backend/reports/
```

## 2. Environment

```bash
cd ~/SI-studio/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

matplotlib is the slow one; expect a couple of minutes.

## 3. Config

Every setting in `app/config.py` has a default and no broker is needed, so
`.env` is optional here. Set a real token anyway rather than shipping the
default `dev-token-change-me`:

```bash
printf 'API_TOKEN=pick-something-here\n' > .env
```

Not needed for viewing: RabbitMQ, `secrets/`, `shards/`, SSH keys.

## 4. Build the website

```bash
python tools/build_web.py
```

Unpacks `../split-inference-pipeline.html` into `backend/web/` — the page, its
vendored React/dc-runtime, its images, and the transport layer from `ui/`.

**This step is mandatory.** `backend/web/` is gitignored on purpose: the HTML
bundle is the single source of truth, so the site is rebuilt rather than
committed.

## 5. Run

Browsing on the target itself:

```bash
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000`. The API token is filled in automatically because
the request comes from loopback.

Browsing from another machine on the LAN:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
sudo ufw allow 8000/tcp        # only if ufw is active
```

Open `http://<target-ip>:8000`, then **click the connection chip in the header
and paste the `API_TOKEN`**. Remote browsers deliberately get a blank token —
`runtime-config.js` reads the client address off the socket, not from a
forwardable header, so binding to `0.0.0.0` never hands the token to visitors.

## 6. Verify

```bash
curl -s localhost:8000/health
# {"status":"ok","broker_connected":false,...}
```

`broker_connected: false` is correct for this setup. The broker is optional at
boot — the Simulate path and the whole Control tab work without RabbitMQ, and
`/run/start` reconnects on demand.

```bash
curl -s localhost:8000/reports -H "Authorization: Bearer pick-something-here"
```

Should list every report directory that came down with the clone.

---

## Moving new reports across later

Reports are self-contained folders, so git carries them:

```bash
# source machine
git add backend/reports && git commit -m "new run" && git push

# target
cd ~/SI-studio && git pull
```

Refresh the browser. New reports appear, sorted newest-first by the `created_at`
in each manifest — not by folder name, since `2807` sorts before `0108` but is a
month later.

To move them without git:

```bash
rsync -av backend/reports/ user@target:~/SI-studio/backend/reports/
```

### Redact credentials first

Runs archive their `config.yaml` under `reports/<id>/logs/`, and that file
contains the broker block in plaintext:

```yaml
rabbit:
  address: 192.168.101.91
  username: "machine-1"
  password: "123456"
```

Replace the password before committing. Nothing breaks: `runlog.read_config`
keeps only numeric `key: value` pairs, and of those the charts display just
`batch_size`, `num_bit` and `window_batches`.

```bash
sed -i 's/^\(  password: \).*$/\1"REDACTED"/' backend/reports/*/logs/config.yaml
```

---

## What is never in git

| Excluded | Why |
|---|---|
| `backend/.venv/` | 221 MB; recreate with `python3 -m venv` |
| `backend/.env` | Real `API_TOKEN`, `BROKER_URL`, `RABBITMQ_MGMT_PASSWORD` |
| `backend/secrets/` | `.master.key` and the Fernet-encrypted `credentials.json` |
| `backend/split_inference.db` | Local state, recreated on first run |
| `backend/web/` | Generated by `tools/build_web.py` |

`.env` and `secrets/` must be carried across by hand if you need them.

---

## Full deployment (live runs, SSH, real inference)

Additional steps beyond the above:

1. Copy `backend/secrets/` across by hand (SSH keys + encrypted password store).
2. Fill in `.env` properly — `BROKER_URL`, `DEVICE_BROKER_URL`,
   `RABBITMQ_MGMT_*`. `DEVICE_BROKER_URL` must be an address the *devices* can
   reach, not `localhost`.
3. Bring up RabbitMQ, or use Docker for the whole thing:

   ```bash
   cd ~/SI-studio/backend
   API_TOKEN=your-real-token docker compose up -d --build
   ```

   The build context is the repo root so the UI bundle is in scope and the site
   is baked into the image — no `build_web.py` step.

   **Before relying on this, add a reports volume.** `docker-compose.yml` mounts
   `/data`, `/secrets` and `/shards` but not reports, so `reports_dir` resolves
   to an ephemeral `/app/reports` and every saved report is lost on the next
   `up --build`:

   ```yaml
       volumes:
         - backend-data:/data
         - ./secrets:/secrets
         - ./shards:/shards
         - ./reports:/app/reports      # add this
   ```

Then follow `backend/README.md` § "Running split inference for real".
