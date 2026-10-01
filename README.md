# es-metrics-exporter

Counts log lines in Elasticsearch and exposes the counts as Prometheus metrics.

To be honest, this is a workaround. Some of the signals we needed existed only in logs, and all alerting already lived in VictoriaMetrics and Alertmanager. Instead of running a second alerting system on top of Elasticsearch, this exporter runs a list of Lucene queries once a minute and publishes how many documents matched.

If you can add real metrics to the application, do that. If you control the log shipper, converting logs to metrics there (Vector, Fluent Bit, mtail) is cheaper because it doesn't query Elasticsearch at all. This tool is for the case when neither is possible.

Keep in mind:

- every metric is one query per cycle, so dozens are fine and thousands are not
- if log ingestion lags, the numbers lag with it
- values are counts over a sliding window (gauges), `rate()` makes no sense on them

Works with OpenSearch too.

## Running

```bash
cp config.example.yaml config.yaml
docker build -t es-metrics-exporter .
docker run --rm -p 9876:9876 \
  -e ELASTIC_USER=user -e ELASTIC_PASSWORD=pass \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  es-metrics-exporter

curl -s localhost:9876/metrics | grep app_
```

## Config

Metrics are described in YAML, adding one doesn't require touching the code. The path comes from `CONFIG_PATH` (default `/app/config.yaml`).

```yaml
exporter:
  port: 9876
  collect_interval: 60        # seconds
  timezone: UTC               # for working_hours_*

elasticsearch:
  url: https://elasticsearch.example.com:9200
  index: app-logs-*           # default, can be set per metric
  timestamp_field: "@timestamp"
  timeout: 30
  verify_ssl: true
  # ca_bundle: /etc/ssl/custom/ca.crt

metrics:
  - name: app_payment_errors_5m
    description: Error log lines of the payment service in the last 5 minutes
    query: 'service:"payment" AND level:"ERROR"'
    lookback_minutes: 5

  - name: app_export_job_runs_1h
    query: 'message:"export finished"'
    index: batch-logs-*
    lookback_minutes: 60
    working_hours_only: true
    working_hours_start: 6
    working_hours_end: 22
    off_hours_value: 1
```

Only `elasticsearch.url`, an index and `name` + `query` for each metric are mandatory. The config is checked at startup.

Credentials are taken from the environment: `ELASTIC_API_KEY`, or `ELASTIC_USER` with `ELASTIC_PASSWORD`.

`working_hours_only` exists for heartbeat alerts like "the job hasn't logged anything for an hour". If the job only runs during the day, such an alert fires every night. With this option the query is skipped outside the given hours and the metric is set to `off_hours_value`.

## Metrics

Besides the configured ones:

- `es_exporter_query_success{metric}`: 1 or 0 for the last query
- `es_exporter_query_duration_seconds{metric}`
- `es_exporter_last_collect_timestamp_seconds`

When a query fails the metric becomes `NaN`, not `0`. Otherwise an Elasticsearch outage would look exactly like "no errors in the logs". So alert on the exporter as well:

```yaml
- alert: PaymentErrors
  expr: app_payment_errors_5m > 10
  for: 5m

- alert: EsExporterQueryFailing
  expr: es_exporter_query_success == 0
  for: 10m

- alert: EsExporterStalled
  expr: time() - es_exporter_last_collect_timestamp_seconds > 300
```

## Deployment

Stateless. You can run two replicas, but each of them runs every query, so Elasticsearch gets twice the load; use `max by (...)` in alert rules in that case. Example with a ConfigMap: [deploy/kubernetes.yaml](deploy/kubernetes.yaml).

Multi-arch image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/es-metrics-exporter:<tag> --push .
```

Tests: `pip install -r requirements.txt pytest && pytest`

## License

MIT
