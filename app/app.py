#!/usr/bin/env python3
"""
Configurable Elasticsearch log-count exporter for Prometheus / VictoriaMetrics.
Metrics are described in a YAML config, so a new metric needs no code change.
"""

import os
import signal
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import yaml
from prometheus_client import start_http_server, Gauge

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

gauges = {}
query_success = Gauge("es_exporter_query_success", "1 if the last query for the metric succeeded", ["metric"])
query_duration = Gauge("es_exporter_query_duration_seconds", "Duration of the last query for the metric", ["metric"])
last_collect = Gauge("es_exporter_last_collect_timestamp_seconds", "Unix time of the last finished collect cycle")

stop_event = threading.Event()


def load_config(path=CONFIG_PATH):
    with open(path, 'r') as f:
        config = yaml.safe_load(f) or {}

    es_config = config.get('elasticsearch') or {}
    if not es_config.get('url'):
        raise ValueError("elasticsearch.url is required")

    metrics = config.get('metrics') or []
    if not metrics:
        raise ValueError("at least one metric is required")

    names = set()
    for metric_config in metrics:
        for field in ('name', 'query'):
            if not metric_config.get(field):
                raise ValueError(f"metric is missing '{field}': {metric_config}")
        if metric_config['name'] in names:
            raise ValueError(f"duplicate metric name: {metric_config['name']}")
        names.add(metric_config['name'])
        if not (metric_config.get('index') or es_config.get('index')):
            raise ValueError(f"no index for metric {metric_config['name']}")

    config['elasticsearch'] = es_config
    config.setdefault('exporter', {})
    ZoneInfo(config['exporter'].get('timezone', 'UTC'))
    return config


def create_gauge(metric_config):
    name = metric_config['name']
    description = metric_config.get('description', name)
    gauges[name] = Gauge(name, description)
    logger.info(f"Registered metric: {name}")


def count_query(es_config, lucene_query, lookback_minutes, index=None):
    """Returns the number of matching documents, or None if the query failed."""
    timestamp_field = es_config.get('timestamp_field', '@timestamp')

    query = {
        "query": {
            "bool": {
                "filter": [
                    {"query_string": {"query": lucene_query}},
                    {"range": {timestamp_field: {"gte": f"now-{lookback_minutes}m", "lte": "now"}}}
                ]
            }
        }
    }

    url = f"{es_config['url'].rstrip('/')}/{index or es_config['index']}/_count"

    headers = {"Content-Type": "application/json"}
    auth = None
    if os.getenv("ELASTIC_API_KEY"):
        headers["Authorization"] = f"ApiKey {os.getenv('ELASTIC_API_KEY')}"
    elif os.getenv("ELASTIC_USER") and os.getenv("ELASTIC_PASSWORD"):
        auth = (os.getenv("ELASTIC_USER"), os.getenv("ELASTIC_PASSWORD"))

    verify = es_config.get('ca_bundle') or es_config.get('verify_ssl', True)

    try:
        resp = requests.post(
            url,
            json=query,
            auth=auth,
            headers=headers,
            timeout=es_config.get('timeout', 30),
            verify=verify
        )
        resp.raise_for_status()
        return int(resp.json()["count"])

    except Exception as e:
        logger.error(f"Query failed for '{lucene_query}': {e}")
        return None


def is_working_hours(start_hour=6, end_hour=22, timezone="UTC"):
    now = datetime.now(ZoneInfo(timezone))
    return start_hour <= now.hour < end_hour


def collect_metrics(config):
    es_config = config['elasticsearch']
    timezone = config['exporter'].get('timezone', 'UTC')

    for metric_config in config['metrics']:
        name = metric_config['name']
        query = metric_config['query']
        lookback = metric_config.get('lookback_minutes', 60)

        if metric_config.get('working_hours_only', False):
            if not is_working_hours(
                metric_config.get('working_hours_start', 6),
                metric_config.get('working_hours_end', 22),
                timezone
            ):
                # outside working hours a fixed value keeps "no logs" alerts quiet
                gauges[name].set(metric_config.get('off_hours_value', 1))
                continue

        started = time.monotonic()
        count = count_query(es_config, query, lookback, metric_config.get('index'))
        query_duration.labels(metric=name).set(time.monotonic() - started)

        if count is None:
            # NaN instead of 0, so a failed query is not mistaken for "no logs"
            gauges[name].set(float('nan'))
            query_success.labels(metric=name).set(0)
        else:
            gauges[name].set(count)
            query_success.labels(metric=name).set(1)

    last_collect.set_to_current_time()
    logger.info(f"Collected {len(config['metrics'])} metrics")


def main():
    config = load_config()

    for metric_config in config['metrics']:
        create_gauge(metric_config)

    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    port = config['exporter'].get('port', 9876)
    interval = config['exporter'].get('collect_interval', 60)
    start_http_server(port)
    logger.info(f"Exporter started on port {port}, interval {interval}s")

    collect_metrics(config)

    while not stop_event.wait(interval):
        collect_metrics(config)

    logger.info("Shutting down")


if __name__ == "__main__":
    main()
