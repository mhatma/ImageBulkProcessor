import os, json, asyncio, signal, tempfile
from pathlib import Path
from io import BytesIO
import aio_pika, aiohttp
from aio_pika import ExchangeType
from PIL import Image
from opentelemetry.propagate import extract
from telemetry_worker import setup_tracing

RABBIT_URL  = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
EXCHANGE    = os.getenv("EXCHANGE", "images")
QUEUE       = os.getenv("QUEUE", "images.q")
DLX         = os.getenv("DLX", "images.dlx")
DLQ         = os.getenv("DLQ", "images.dlx.q")
ROUTING_KEY = os.getenv("ROUTING_KEY", "process_image")
DATA_DIR    = Path(os.getenv("DATA_DIR", "/data"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))

tracer = setup_tracing("worker")

def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)

async def ensure_topology(channel):
    ex  = await channel.declare_exchange(EXCHANGE, ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX, ExchangeType.DIRECT, durable=True)
    dlq = await channel.declare_queue(DLQ, durable=True)
    await dlq.bind(dlx, ROUTING_KEY)
    q = await channel.declare_queue(
        QUEUE, durable=True,
        arguments={"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": ROUTING_KEY}
    )
    await q.bind(ex, ROUTING_KEY)
    return ex, q

async def process_image(url: str, dest: Path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            raw = await resp.read()
    img = Image.open(BytesIO(raw)).convert("RGB")
    # simple operation: thumbnail (width 400px)
    img.thumbnail((400, 400))
    out = BytesIO()
    img.save(out, format="JPEG", quality=85)
    atomic_write(dest, out.getvalue())

async def handle(message: aio_pika.IncomingMessage, sem: asyncio.Semaphore):
    async with sem:
        ctx = extract(message.headers or {})
        with tracer.start_as_current_span("handle_task", context=ctx) as span:
            try:
                payload = json.loads(message.body)
                task_id = payload["task_id"]
                url     = payload["url"]
                img_id  = payload.get("id") or "noid"
                span.set_attribute("task.id", task_id)
                span.set_attribute("task.url", url)
                dest = DATA_DIR / f"{img_id}.jpg"
                if dest.exists():
                    await message.ack()
                    return
                with tracer.start_as_current_span("download_transform_write"):
                    await process_image(url, dest)
                    await message.ack()
                    return
                return
            except Exception as e:
                # Poison to DLQ (no requeue)
                await message.reject(requeue=False)
                return

async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = await aio_pika.connect_robust(RABBIT_URL)
    chan = await conn.channel()
    await chan.set_qos(prefetch_count=16)
    _, queue = await ensure_topology(chan)

    sem = asyncio.Semaphore(CONCURRENCY)
    await queue.consume(lambda msg: asyncio.create_task(handle(msg, sem)))

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    await chan.close()
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
