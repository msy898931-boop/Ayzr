"""Gunicorn configuration for production.

    gunicorn -c gunicorn.conf.py wsgi:app

`sync` workers are fine here: requests are short and mostly I/O-bound on the
model/market calls. The timeout is generous because a `full` analysis makes
twelve sequential model calls.
"""
import multiprocessing
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = int(os.environ.get("WEB_CONCURRENCY", min(4, multiprocessing.cpu_count() * 2 + 1)))
threads = int(os.environ.get("WEB_THREADS", 4))
worker_class = "gthread"
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# Never trust client-supplied X-Forwarded-* unless a proxy sets them.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
