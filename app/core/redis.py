"""Redis connection for task queue."""

from redis import Redis
from rq import Queue

from config.settings import settings

redis_conn = Redis.from_url(settings.redis_url)

# Task queues with different priorities
tg_download_queue = Queue("tg_download", connection=redis_conn)
external_download_queue = Queue("external_download", connection=redis_conn)
retry_queue = Queue("retry", connection=redis_conn)
