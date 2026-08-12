from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "supportguard_http_requests_total", "HTTP requests", ("method", "route", "status")
)
HTTP_LATENCY = Histogram(
    "supportguard_http_request_duration_seconds", "HTTP request latency", ("route",)
)
GRAPH_RUNS = Counter("supportguard_graph_runs_total", "Graph segments", ("result",))
AGENT_DECISIONS = Counter(
    "supportguard_agent_decisions_total", "Agent decisions", ("decision_type",)
)
TOOL_OBSERVATIONS = Counter(
    "supportguard_tool_observations_total", "Normalized tool observations", ("tool", "status")
)
OUTBOX_PUBLISH = Counter(
    "supportguard_outbox_publish_total", "Outbox publish outcomes", ("result",)
)
RECONCILE = Counter(
    "supportguard_runtime_reconcile_total", "Runtime jobs reconciled", ("reason",)
)
JOB_OUTCOMES = Counter(
    "supportguard_runtime_job_outcome_total", "Runtime job outcomes", ("outcome",)
)
ATTEMPT_OUTCOMES = Counter(
    "supportguard_agent_call_attempt_total", "Durable external call attempts", ("kind", "status")
)
ATTEMPT_LATENCY = Histogram(
    "supportguard_agent_call_attempt_duration_seconds",
    "External call latency",
    ("kind",),
)
APPROVAL_DECISIONS = Counter(
    "supportguard_approval_decision_total", "Human approval decisions", ("decision", "reused")
)
SSE_CONNECTIONS = Gauge("supportguard_sse_connections", "Current SSE connections")
SSE_REPLAYED_EVENTS = Counter(
    "supportguard_sse_replayed_events_total", "Durable events replayed to SSE clients"
)
