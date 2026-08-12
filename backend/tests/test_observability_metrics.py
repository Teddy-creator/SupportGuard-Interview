from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from supportguard.main import create_app
from supportguard.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS


def test_supportguard_metrics_use_only_frozen_low_cardinality_labels() -> None:
    forbidden = {
        "tenant_id",
        "customer_id",
        "ticket_id",
        "run_id",
        "job_id",
        "approval_id",
        "url",
        "path",
    }
    collectors = [
        collector
        for collector in REGISTRY._collector_to_names  # type: ignore[attr-defined]
        if any(name.startswith("supportguard_") for name in REGISTRY._collector_to_names[collector])
    ]
    labels = {
        label
        for collector in collectors
        for label in getattr(collector, "_labelnames", ())
    }
    assert labels.isdisjoint(forbidden)


def test_dynamic_resource_ids_share_one_http_route_label() -> None:
    unique_ids = ("ticket_metric_cardinality_alpha", "ticket_metric_cardinality_beta")
    with TestClient(create_app(testing=True)) as client:
        for ticket_id in unique_ids:
            response = client.get(f"/api/tickets/{ticket_id}")
            assert response.status_code == 401

    request_routes = {
        sample.labels["route"]
        for metric in HTTP_REQUESTS.collect()
        for sample in metric.samples
        if sample.name == "supportguard_http_requests_total"
    }
    latency_routes = {
        sample.labels["route"]
        for metric in HTTP_LATENCY.collect()
        for sample in metric.samples
        if sample.name == "supportguard_http_request_duration_seconds_count"
    }
    assert "/tickets/{ticket_id}" in request_routes
    assert "/tickets/{ticket_id}" in latency_routes
    assert request_routes.isdisjoint(unique_ids)
    assert latency_routes.isdisjoint(unique_ids)
