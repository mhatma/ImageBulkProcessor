# Image Tasks POC (FastAPI + RabbitMQ + Workers + OpenTelemetry)

A small, proof‑of‑concept that publishes “image processing” jobs to RabbitMQ from a FastAPI service, consumes them with asyncio workers, and emits distributed traces to Jaeger via the OpenTelemetry Collector. Runs locally via Docker Compose and can be deployed to Kubernetes (with optional KEDA autoscaling).

By default the active (and only) image processing job is creating thumbnails of random images from picsum. 

---

**Delivery semantics:** at‑least‑once. Workers are idempotent (atomic writes; skip if output exists).  
**Backpressure:** RabbitMQ `prefetch_count` + per‑worker `CONCURRENCY`.  
**Failure handling:** messages rejected with `requeue=False` are dead‑lettered to the DLQ.

---

## Services (Docker Compose)

- **postgres** – holds `images(id,url,processed)`; seeded with ~300 placeholder rows from `db/init.sql`.
- **rabbitmq** – broker + management UI (http://localhost:15672). Credentials come from `.env`.
- **api** – FastAPI producer.
  - `POST /enqueue?limit=N` pulls N URLs from Postgres and enqueues messages.
  - `POST /tasks` enqueues a single task payload `{url, ops}`.
  - `GET /healthz` liveness/readiness.
- **worker** – asyncio consumer.
  - `prefetch_count=16`; parallelism via `CONCURRENCY` (default 8).
  - Downloads the image, makes a thumbnail, **atomically** writes to `/data/<id>.jpg`.
  - `ack` on success; `reject(requeue=false)` on failure (→ DLQ).
- **collector** – OpenTelemetry Collector (receives OTLP on 4318 and forwards to Jaeger).
- **jaeger** – Trace UI at http://localhost:16686 (search for service `api` or `worker`).

RabbitMQ topology (declared by the worker):
- Exchange: `images` (direct)
- Queue: `images.q` (durable)
- Dead‑letter exchange: `images.dlx` (direct)
- Dead‑letter queue: `images.dlx.q` (bound to `images.dlx`)

---

## Prerequisites

- Docker + Docker Compose
- (Optional) `curl` for quick testing

Create a `.env` in the project root (values below are examples; adjust as needed):

```dotenv
POSTGRES_USER=app
POSTGRES_PASSWORD=app-pass
POSTGRES_DB=app

RABBIT_USER=app
RABBIT_PASSWORD=app-pass

# OTLP HTTP endpoint exposed by the Collector container
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318/v1/traces
```

---

## Quick start (local)

```bash
# 1) Build & start
docker compose up -d --build

# 2) Enqueue ~300 image tasks from Postgres
curl -X POST http://localhost:8000/enqueue?limit=300

# 3) Watch the worker drain the queue
docker compose logs -f worker

# 4) Inspect outputs written by workers
docker compose exec worker sh -lc 'ls -l /data | head -n 20; echo "..."'

# 5) UIs
# RabbitMQ UI (queue depths, DLQ, etc.)
open http://localhost:15672
# Jaeger UI (distributed traces)
open http://localhost:16686
```

**Scaling locally:** run more workers to drain faster:
```bash
docker compose up -d --scale worker=4
```

---

## Endpoints (API)

- `POST /enqueue?limit=N` – batch enqueue from Postgres (default limit 200 if not provided).
- `POST /tasks` – body: `{"url": "https://...", "ops": ["thumbnail"]}`.
- `GET /healthz` – returns `{"ok": true}`.

Example:
```bash
curl -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"url":"https://picsum.photos/seed/demo/1024/768","ops":["thumbnail"]}'
```

---

## Observability

Both services export OpenTelemetry traces to the Collector via **OTLP/HTTP** (`$OTEL_EXPORTER_OTLP_ENDPOINT`), and the Collector forwards to Jaeger.

In Jaeger you should see a single trace linking:
- `/enqueue` (FastAPI) → publish
- consumer `handle_task` (worker) → download/transform/write

If you don’t see traces, verify:
- The Collector container is healthy.
- `collector-config.yaml` is actually mounted at `/etc/otelcol/config.yaml`.
- The `OTEL_EXPORTER_OTLP_ENDPOINT` env var in `api` and `worker` is set to `http://collector:4318/v1/traces`.

---

## RabbitMQ dead‑letter queues (DLQ)

The main queue `images.q` is created with `x-dead-letter-exchange=images.dlx` and routed to `images.dlx.q`. Messages are dead‑lettered when the worker `rejects(requeue=False)` (e.g., unrecoverable error), when TTL expires, or if the queue overflows. Inspect DLQ via the RabbitMQ UI and optionally replay them after fixing issues.

---

## Kubernetes (optional)

The repository includes example manifests to run the same stack on Kubernetes and scale workers with **KEDA** based on queue backlog:

- **PVC** for worker outputs (`/data`)
- **Deployments** for `api` and `worker`
- **ScaledObject** (KEDA) targeting `images.q` with `mode: QueueLength` (messages per replica)

Apply manifests, port‑forward the `api` service, then hit `/enqueue` as above.

---

## Common environment variables

- `RABBIT_URL` (default `amqp://<user>:<pass>@rabbitmq:5672/`)
- `EXCHANGE=images`, `QUEUE=images.q`, `DLX=images.dlx`, `DLQ=images.dlx.q`, `ROUTING_KEY=process_image`
- `DATABASE_URL` (api) – e.g., `postgres://app:app-pass@postgres:5432/app`
- `CONCURRENCY` (worker) – parallel downloads within one worker process (default 8)
- `OTEL_EXPORTER_OTLP_ENDPOINT` – default `http://collector:4318/v1/traces`

---

## Troubleshooting

- **Collector complains about `logging` exporter**  
  The `logging` exporter is deprecated. Use `otlphttp` → Jaeger and (optionally) the `debug` exporter, or keep a minimal pipeline (see `otel/collector-config.yaml`).

- **Collector doesn’t seem to read my config**  
  Ensure the bind mount path in `docker-compose.yml` matches your file location. From the host:
  ```bash
  docker compose exec collector sh -lc 'ls -l /etc/otelcol && echo "---" && sed -n "1,120p" /etc/otelcol/config.yaml'
  ```

- **No traces in Jaeger**  
  Hit `/enqueue`, watch `collector` logs, and confirm the Jaeger UI is at http://localhost:16686. Verify `COLLECTOR_OTLP_ENABLED=true` is set on Jaeger (in compose).

- **Messages pile up**  
  Increase worker replicas (`--scale worker=4`) or raise `CONCURRENCY`; tune RabbitMQ `prefetch_count`.

- **DLQ grows**  
  Investigate `x-death` headers, fix the root cause, and build a small “DLQ replayer” to republish after correction.

---

## License

MIT (or your preferred license).
