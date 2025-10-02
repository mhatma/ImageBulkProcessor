import os, json, uuid, asyncio
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import aio_pika, asyncpg
from aio_pika import Message, DeliveryMode, ExchangeType
from telemetry_api import setup_tracing
from opentelemetry.propagate import inject

RABBIT_URL  = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
EXCHANGE    = os.getenv("EXCHANGE", "images")
ROUTING_KEY = os.getenv("ROUTING_KEY", "process_image")
DATABASE_URL= os.getenv("DATABASE_URL", "postgres://app:app-pass@postgres:5432/app")

app = FastAPI(title="image-processor")
tracer = setup_tracing(app, "api")

class ImageTask(BaseModel):
    id: Optional[int] = None
    url: str = Field(..., min_length=8)
    ops: list[str] = Field(default_factory=lambda: ["thumbnail"])
    quality: int | None = Field(default=None, ge=1, le=95)

_conn = _chan = _ex = None
_db = None

@app.on_event("startup")
async def startup():
    global _conn, _chan, _ex, _db
    _conn = await aio_pika.connect_robust(RABBIT_URL)
    _chan = await _conn.channel(publisher_confirms=True)
    _ex = await _chan.declare_exchange(EXCHANGE, ExchangeType.DIRECT, durable=True)
    _db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)

@app.on_event("shutdown")
async def shutdown():
    await _chan.close()
    await _conn.close()
    await _db.close()

@app.get("/healthz")
async def healthz():
    return {"ok": True}

async def publish(task: ImageTask):
    task_id = str(uuid.uuid4())
    body = json.dumps({"task_id": task_id, "id": task.id, "url": task.url, "ops": task.ops, "quality": task.quality}).encode()
    headers = {}
    inject(headers)  # W3C trace context
    msg = Message(body, delivery_mode=DeliveryMode.PERSISTENT, content_type="application/json",
                  headers={**headers, "task_id": task_id})
    await _ex.publish(msg, routing_key=ROUTING_KEY, mandatory=True)
    return task_id

@app.post("/tasks")
async def enqueue_one(task: ImageTask):
    try:
        task_id = await publish(task)
        return {"task_id": task_id, "status": "enqueued"}
    except Exception as e:
        raise HTTPException(503, f"Publish failed: {e}")

@app.post("/enqueue")
async def enqueue_from_db(limit: int = Query(200, ge=1, le=1000), quality: int = Query(85, ge=1, le=95)):
    rows = await _db.fetch("SELECT id, url FROM images WHERE processed = FALSE LIMIT $1", limit)
    if not rows:
        return {"enqueued": 0}
    count = 0
    for r in rows:
        await publish(ImageTask(id=r["id"], url=r["url"], quality=quality))
        count += 1
        # small yield to build backlog more smoothly (optional)
        await asyncio.sleep(0)
    return {"enqueued": count}
