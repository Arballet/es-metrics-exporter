import math
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import yaml
from prometheus_client import REGISTRY

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import app as exporter  # noqa: E402

ES = {"url": "https://es.example.com:9200/", "index": "logs-*"}


def write_config(tmp_path, config):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


def value(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


@pytest.fixture
def post(monkeypatch):
    mock = MagicMock()
    mock.return_value.json.return_value = {"count": 7}
    monkeypatch.setattr(exporter.requests, "post", mock)
    for var in ("ELASTIC_API_KEY", "ELASTIC_USER", "ELASTIC_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return mock


def test_example_config_is_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "config.example.yaml")
    config = exporter.load_config(path)
    assert len(config["metrics"]) == 2


@pytest.mark.parametrize("config, error", [
    ({"metrics": [{"name": "a", "query": "x"}]}, "elasticsearch.url"),
    ({"elasticsearch": ES}, "at least one metric"),
    ({"elasticsearch": ES, "metrics": [{"name": "a"}]}, "missing 'query'"),
    ({"elasticsearch": ES, "metrics": [{"name": "a", "query": "x"}, {"name": "a", "query": "y"}]}, "duplicate"),
    ({"elasticsearch": {"url": "https://es"}, "metrics": [{"name": "a", "query": "x"}]}, "no index"),
])
def test_invalid_config(tmp_path, config, error):
    with pytest.raises(ValueError, match=error):
        exporter.load_config(write_config(tmp_path, config))


def test_count_query_request(post):
    assert exporter.count_query(ES, 'level:"ERROR"', 5) == 7

    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://es.example.com:9200/logs-*/_count"
    assert kwargs["verify"] is True
    assert kwargs["auth"] is None
    filters = kwargs["json"]["query"]["bool"]["filter"]
    assert filters[0] == {"query_string": {"query": 'level:"ERROR"'}}
    assert filters[1] == {"range": {"@timestamp": {"gte": "now-5m", "lte": "now"}}}


def test_count_query_index_override_and_auth(post, monkeypatch):
    monkeypatch.setenv("ELASTIC_USER", "user")
    monkeypatch.setenv("ELASTIC_PASSWORD", "pass")
    exporter.count_query({**ES, "ca_bundle": "/ca.crt"}, "x", 5, index="other-*")
    assert post.call_args.args[0].endswith("/other-*/_count")
    assert post.call_args.kwargs["auth"] == ("user", "pass")
    assert post.call_args.kwargs["verify"] == "/ca.crt"


def test_count_query_api_key(post, monkeypatch):
    monkeypatch.setenv("ELASTIC_API_KEY", "key")
    exporter.count_query(ES, "x", 5)
    assert post.call_args.kwargs["headers"]["Authorization"] == "ApiKey key"


def test_count_query_failure_returns_none(post):
    post.side_effect = Exception("boom")
    assert exporter.count_query(ES, "x", 5) is None


def test_collect_sets_values_and_marks_failures(post, monkeypatch):
    config = {
        "exporter": {},
        "elasticsearch": ES,
        "metrics": [{"name": "test_ok_total", "query": "x"}, {"name": "test_failed_total", "query": "y"}],
    }
    for metric_config in config["metrics"]:
        exporter.create_gauge(metric_config)

    monkeypatch.setattr(exporter, "count_query", lambda es, query, *a: 7 if query == "x" else None)
    exporter.collect_metrics(config)

    assert value("test_ok_total") == 7
    assert value("es_exporter_query_success", {"metric": "test_ok_total"}) == 1
    assert math.isnan(value("test_failed_total"))
    assert value("es_exporter_query_success", {"metric": "test_failed_total"}) == 0
    assert value("es_exporter_last_collect_timestamp_seconds") > 0


def test_off_hours_value_skips_query(post, monkeypatch):
    config = {
        "exporter": {"timezone": "Europe/Berlin"},
        "elasticsearch": ES,
        "metrics": [{"name": "test_night_total", "query": "x", "working_hours_only": True, "off_hours_value": 1}],
    }
    exporter.create_gauge(config["metrics"][0])
    monkeypatch.setattr(exporter, "is_working_hours", lambda *a: False)

    exporter.collect_metrics(config)

    assert value("test_night_total") == 1
    post.assert_not_called()


def test_is_working_hours_uses_timezone(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 15, 5, 30, tzinfo=ZoneInfo("UTC")).astimezone(tz)

    monkeypatch.setattr(exporter, "datetime", FixedDatetime)
    assert exporter.is_working_hours(6, 22, "UTC") is False
    assert exporter.is_working_hours(6, 22, "Europe/Berlin") is True
