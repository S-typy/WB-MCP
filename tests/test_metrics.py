"""Метрики Prometheus."""

from wbmcp.metrics import Metrics, default_metrics


def test_counters_and_summaries_render():
    metrics = Metrics(prefix="t")
    assert metrics.render() == ""
    metrics.describe("calls_total", "вызовы")
    metrics.inc("calls_total", {"seller": "main", "outcome": "ok"})
    metrics.inc("calls_total", {"outcome": "ok", "seller": "main"})
    metrics.inc("calls_total", {"seller": "x\"y", "outcome": "err"}, value=2)
    metrics.inc("plain")
    metrics.observe("wait_seconds", 0.5, {"seller": "main"})
    metrics.observe("wait_seconds", 1.5, {"seller": "main"})
    text = metrics.render()
    assert "# HELP t_calls_total вызовы" in text
    assert "# TYPE t_calls_total counter" in text
    assert 't_calls_total{outcome="ok",seller="main"} 2' in text
    assert 't_calls_total{outcome="err",seller="x\\"y"} 2' in text
    assert "t_plain 1" in text
    assert "# TYPE t_wait_seconds summary" in text
    assert 't_wait_seconds_sum{seller="main"} 2' in text
    assert 't_wait_seconds_count{seller="main"} 2' in text
    assert metrics.counter("calls_total", {"seller": "main", "outcome": "ok"}) == 2
    assert metrics.counter("missing") == 0
    assert text.endswith("\n")


def test_default_metrics_have_help():
    text = default_metrics()
    text.inc("calls_total", {"seller": "a", "section": "orders-fbs", "outcome": "ok"})
    rendered = text.render()
    assert "# HELP wbmcp_calls_total" in rendered
    assert 'wbmcp_calls_total{outcome="ok",section="orders-fbs",seller="a"} 1' in rendered
