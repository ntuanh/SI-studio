FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# `ping` for the latency probe; iperf3 for the bandwidth probe. Both are used by
# POST /devices/{id}/probe -- on the *device* side, but having the client here
# lets you sanity-check reachability from the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping iperf3 \
 && rm -rf /var/lib/apt/lists/*

# The build context is the *repo root*, not backend/, so the UI bundle beside
# the backend can be baked into the image. Build it by hand with:
#   docker build -f backend/Dockerfile -t split-backend .
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY backend/agent ./agent
COPY backend/tools ./tools
COPY backend/ui ./ui

# Build the website into /app/web. The bracket glob makes the bundle optional:
# without it the image still runs and `/` explains how to build, so a missing
# UI is a degraded image rather than a failed build.
COPY split-inference-pipeline.htm[l] ./
RUN if [ -f split-inference-pipeline.html ]; then \
      python tools/build_web.py --source split-inference-pipeline.html --out ./web; \
    else \
      echo "no UI bundle in the build context; / will explain how to build it"; \
    fi

RUN mkdir -p /data /secrets /shards

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
