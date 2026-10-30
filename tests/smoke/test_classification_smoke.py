"""Live smoke tests for the classification service (P1 Task 14).

These run against a DEPLOYED service (alpha, amd64) and are gated on
``CLASSIFICATION_SMOKE_ENDPOINT`` (``host:port`` of the gRPC listener). Without it every
test SKIPS so the suite collects and runs cleanly with no cluster; WITH it set nothing
skips -- a missing prerequisite (kubectl, token, sink traffic) is a FAILURE, never a skip.

Environment (no secrets are ever hard-coded):

* ``CLASSIFICATION_SMOKE_ENDPOINT``     gRPC ``host:port`` -- the on/off switch.
* ``CLASSIFICATION_SMOKE_TOKEN``        bearer JWT carrying the classification scope.
* ``CLASSIFICATION_SMOKE_CA_FILE``      PEM root CA for TLS (omit + set ``_INSECURE=1`` for alpha).
* ``CLASSIFICATION_SMOKE_INSECURE``     ``1`` = plaintext channel (alpha ``allowInsecure`` only).
* ``CLASSIFICATION_SMOKE_NAMESPACE``    k8s namespace (default ``waddleai``).
* ``CLASSIFICATION_SMOKE_KUBE_CONTEXT`` optional kubectl context.
* ``CLASSIFICATION_SMOKE_HPA_TARGET``   ``dns:///<headless-svc>.<ns>.svc.cluster.local:50051``
  resolvable from where the test runs (required for the rebalance test).
* ``CLASSIFICATION_SMOKE_OTLP_SINK``    ``host:port`` to bind the OTLP/gRPC test sink
  (default ``0.0.0.0:4317``); the deployment's ``OTEL_EXPORTER_OTLP_ENDPOINT`` must point
  at it for the telemetry test.
* ``CLASSIFICATION_SMOKE_OTEL_WAIT_S``  seconds to wait for metric export (default 150).
* ``CLASSIFICATION_SMOKE_MAX_CONN_AGE_S`` / ``_GRACE_S``  server aging (defaults 300 / 30).
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 - kubectl is the only thing run, with a fixed argv
import time
from concurrent import futures
from dataclasses import dataclass, field

import grpc
import pytest

ENDPOINT = os.environ.get("CLASSIFICATION_SMOKE_ENDPOINT", "").strip()
pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="CLASSIFICATION_SMOKE_ENDPOINT not set (no live deployment)"
)

NAMESPACE = os.environ.get("CLASSIFICATION_SMOKE_NAMESPACE", "waddleai")
KUBE_CONTEXT = os.environ.get("CLASSIFICATION_SMOKE_KUBE_CONTEXT", "")
SELECTOR = "app.kubernetes.io/component=classification"
PY_IN_POD = "/opt/venv/bin/python3"
PERSON_TEXT = "My name is John Smith and I live here."
MAX_CONN_AGE_S = int(os.environ.get("CLASSIFICATION_SMOKE_MAX_CONN_AGE_S", "300"))
GRACE_S = int(os.environ.get("CLASSIFICATION_SMOKE_GRACE_S", "30"))
OTEL_WAIT_S = int(os.environ.get("CLASSIFICATION_SMOKE_OTEL_WAIT_S", "150"))


def _stubs() -> tuple[object, object]:
    """Import the generated protobuf modules lazily so collection never needs them."""
    from services.classification.app.grpc_gen import classification_pb2 as pb
    from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc

    return pb, pb_grpc


def _need(name: str) -> str:
    """Return a required env var or FAIL (the live flag is on, so absence is a defect)."""
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required when CLASSIFICATION_SMOKE_ENDPOINT is set")
    return value


def _channel(
    target: str = ENDPOINT, options: list[tuple[str, object]] | None = None
) -> grpc.Channel:
    """Open a TLS (CA file) or explicitly-insecure channel to ``target``."""
    opts = options or []
    if os.environ.get("CLASSIFICATION_SMOKE_INSECURE", "") == "1":
        return grpc.insecure_channel(target, options=opts)
    ca = _need("CLASSIFICATION_SMOKE_CA_FILE")
    with open(ca, "rb") as fh:
        creds = grpc.ssl_channel_credentials(root_certificates=fh.read())
    return grpc.secure_channel(target, creds, options=opts)


def _auth() -> list[tuple[str, str]]:
    """Build the bearer metadata from the env token (never logged or put in argv)."""
    return [("authorization", f"Bearer {_need('CLASSIFICATION_SMOKE_TOKEN')}")]


def _analyze(channel: grpc.Channel, text: str = PERSON_TEXT, *, authed: bool = True) -> object:
    """Issue one Analyze RPC (authenticated unless ``authed`` is False)."""
    pb, pb_grpc = _stubs()
    stub = pb_grpc.ClassificationServiceStub(channel)  # type: ignore[attr-defined]
    req = pb.AnalyzeRequest(  # type: ignore[attr-defined]
        api_version="v1",
        text=text,
        language="en",
        phase=pb.FILTER_PHASE_INPUT,  # type: ignore[attr-defined]
        org_public_id="00000000-0000-4000-8000-000000000001",
        flag_distinct_id="server",
    )
    return stub.Analyze(req, metadata=_auth() if authed else None, timeout=30)


def _kubectl(*args: str, check: bool = True, timeout: int = 120) -> str:
    """Run kubectl with a fixed argv (no shell); FAIL if kubectl is missing or errors."""
    cmd = ["kubectl"]
    if KUBE_CONTEXT:
        cmd += ["--context", KUBE_CONTEXT]
    cmd += ["-n", NAMESPACE, *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        pytest.fail("kubectl not found on PATH (required for this live smoke test)")
    if check and proc.returncode != 0:
        pytest.fail(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _pods() -> list[str]:
    """Return names of Running, Ready classification pods."""
    out = json.loads(_kubectl("get", "pods", "-l", SELECTOR, "-o", "json"))
    ready: list[str] = []
    for item in out["items"]:
        conds = {c["type"]: c["status"] for c in item["status"].get("conditions", [])}
        if item["status"].get("phase") == "Running" and conds.get("Ready") == "True":
            ready.append(item["metadata"]["name"])
    return sorted(ready)


def _deployment() -> str:
    """Return the classification Deployment name (``deployment.apps/<name>`` form)."""
    name = _kubectl("get", "deploy", "-l", SELECTOR, "-o", "name").split()
    if len(name) != 1:
        pytest.fail(f"expected exactly 1 classification deployment, got {name}")
    return name[0]


def _pod_http(pod: str, path: str) -> tuple[int, str]:
    """GET ``path`` on the pod's loopback HTTP port via kubectl exec; return (status, body)."""
    code = (
        "import urllib.request,urllib.error,sys\n"
        "try:\n"
        f" r=urllib.request.urlopen('http://127.0.0.1:8080{path}',timeout=5)\n"
        " print(r.status);print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        " print(e.code);print(e.read().decode())\n"
    )
    out = _kubectl("exec", pod, "--", PY_IN_POD, "-c", code, check=False)
    head, _, body = out.partition("\n")
    try:
        return int(head.strip()), body
    except ValueError:
        return 0, out


def _requests_total(pod: str) -> float:
    """Sum the pod's ``waddleai_classification_requests_total`` counter from /metrics."""
    _status, body = _pod_http(pod, "/metrics")
    total = 0.0
    for line in body.splitlines():
        m = re.match(
            r"^waddleai_classification_requests(?:_total)?(?:\{[^}]*\})?\s+([0-9.eE+-]+)", line
        )
        if m:
            total += float(m.group(1))
    return total


def test_analyze_returns_person_span() -> None:
    """Authenticated Analyze over the wire returns a PERSON span with OK status (amd64 alpha)."""
    pb, _ = _stubs()
    with _channel() as ch:
        resp = _analyze(ch)
    person = pb.ENTITY_TYPE_PERSON  # type: ignore[attr-defined]
    types = [s.entity_type for s in resp.spans]  # type: ignore[attr-defined]
    print(f"analyze: spans={len(types)} ner_status={resp.ner_status}")  # type: ignore[attr-defined]
    assert person in types, f"no PERSON span in {types}"
    assert resp.ner_status == pb.NER_STATUS_OK  # type: ignore[attr-defined]
    assert not resp.degraded  # type: ignore[attr-defined]


def test_unauthenticated_call_rejected() -> None:
    """A call with no bearer token must be refused with UNAUTHENTICATED (never served)."""
    with _channel() as ch, pytest.raises(grpc.RpcError) as exc:
        _analyze(ch, authed=False)
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_readyz_flips_503_when_pool_killed_and_recovers() -> None:
    """Killing a pod's NER pool workers flips /readyz to 503, then it recovers to 200."""
    pods = _pods()
    assert pods, "no Ready classification pods"
    pod = pods[0]
    status, _ = _pod_http(pod, "/readyz")
    assert status == 200, f"{pod} not ready before the kill (status {status})"
    killer = (
        "import os,signal\n"
        "me=os.getpid();kids={}\n"
        "for p in filter(str.isdigit,os.listdir('/proc')):\n"
        " try: ppid=int(open(f'/proc/{p}/stat').read().rsplit(')',1)[1].split()[1])\n"
        " except OSError: continue\n"
        " kids.setdefault(ppid,[]).append(int(p))\n"
        "leaves=[p for ps in kids.values() for p in ps if p not in kids and p not in (1,me)]\n"
        "[os.kill(p,signal.SIGKILL) for p in leaves]\n"
        "print(len(leaves))\n"
    )
    killed = _kubectl("exec", pod, "--", PY_IN_POD, "-c", killer).strip()
    print(f"killed {killed} leaf worker process(es) in {pod}")
    assert killed.isdigit() and int(killed) >= 1, "no pool worker processes found to kill"
    saw_503, deadline = False, time.monotonic() + 90
    with _channel() as ch:
        while time.monotonic() < deadline:
            try:  # nudge the engine so a broken pool is detected
                _analyze(ch)
            except grpc.RpcError:
                pass
            if _pod_http(pod, "/readyz")[0] == 503:
                saw_503 = True
                break
            time.sleep(1)
    assert saw_503, "/readyz never reported 503 after the pool was killed"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _pod_http(pod, "/readyz")[0] == 200:
            return
        time.sleep(3)
    pytest.fail("/readyz did not recover to 200 within 180s")


@dataclass(slots=True)
class _Sink:
    """Counts of OTLP metric records received by the in-process test sink."""

    metric_points: int = 0
    histograms: int = 0
    requests: int = 0
    names: set[str] = field(default_factory=set)


def _start_sink(bind: str) -> tuple[grpc.Server, _Sink]:
    """Start an OTLP/gRPC metrics sink; a bind failure is a FAILURE (never a skip)."""
    from opentelemetry.proto.collector.metrics.v1 import (
        metrics_service_pb2,
        metrics_service_pb2_grpc,
    )

    sink = _Sink()

    class Svc(metrics_service_pb2_grpc.MetricsServiceServicer):
        """Tally every exported metric / data point / histogram."""

        def Export(self, request, context):  # type: ignore[no-untyped-def]  # noqa: N802
            """Record one ExportMetricsServiceRequest and ack it."""
            sink.requests += 1
            for rm in request.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        sink.names.add(m.name)
                        kind = m.WhichOneof("data")
                        if kind == "histogram":
                            sink.histograms += 1
                        if kind:
                            sink.metric_points += len(getattr(m, kind).data_points)
            return metrics_service_pb2.ExportMetricsServiceResponse()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(Svc(), server)  # type: ignore[no-untyped-call]
    if server.add_insecure_port(bind) == 0:
        pytest.fail(f"OTLP test sink failed to bind {bind}")
    server.start()
    return server, sink


def _log_record_count() -> int:
    """Count structured (JSON) log lines across the classification pods (stdout logging)."""
    out = _kubectl("logs", "-l", SELECTOR, "--tail=200", "--prefix=false", check=False)
    return sum(
        1 for ln in out.splitlines() if ln.strip().startswith("{") and ln.strip().endswith("}")
    )


def test_telemetry_sink_receives_logs_metrics_histogram_with_counts() -> None:
    """Metric points >=1, histograms >=1 reach the OTLP sink and >=1 log record is emitted.

    Counts are printed; a zero count or a sink that fails to start is a FAILURE. Logs are
    counted from pod stdout (the service emits no OTLP log exporter -- see the report).
    """
    server, sink = _start_sink(os.environ.get("CLASSIFICATION_SMOKE_OTLP_SINK", "0.0.0.0:4317"))  # noqa: S104
    try:
        with _channel() as ch:
            for _ in range(5):
                _analyze(ch)
        deadline = time.monotonic() + OTEL_WAIT_S
        while time.monotonic() < deadline and not (sink.metric_points and sink.histograms):
            with _channel() as ch:
                _analyze(ch)
            time.sleep(5)
    finally:
        server.stop(grace=1)
    logs = _log_record_count()
    print(
        f"telemetry counts: log_records={logs} metric_points={sink.metric_points} "
        f"histograms={sink.histograms} export_requests={sink.requests} "
        f"metric_names={sorted(sink.names)}"
    )
    assert logs >= 1, "0 log records emitted"
    assert sink.metric_points >= 1, "0 metric data points received at the sink"
    assert sink.histograms >= 1, "0 histogram metrics received at the sink"


def test_hpa_scale_out_rebalances_clients() -> None:
    """A long-lived client's calls reach >=2 pods after scale 1->3 within MAX_CONNECTION_AGE.

    Proves the headless Service plus server-side connection aging make the client re-resolve
    DNS and spread load instead of staying pinned to the original pod.
    """
    target = _need("CLASSIFICATION_SMOKE_HPA_TARGET")
    pb, _ = _stubs()
    del pb
    dep = _deployment()
    original = int(_kubectl("get", dep, "-o", "jsonpath={.spec.replicas}") or "1")
    hpa = _kubectl("get", "hpa", "-l", SELECTOR, "-o", "name", check=False).split()
    hpa_min = _kubectl("get", hpa[0], "-o", "jsonpath={.spec.minReplicas}") if hpa else ""
    opts: list[tuple[str, object]] = [
        ("grpc.service_config", json.dumps({"loadBalancingConfig": [{"round_robin": {}}]})),
        ("grpc.dns_min_time_between_resolutions_ms", 5000),
    ]
    try:
        _kubectl("scale", dep, "--replicas=1")
        if hpa:
            _kubectl(
                "patch", hpa[0], "--type=merge", "-p", '{"spec":{"minReplicas":1,"maxReplicas":6}}'
            )
        _wait_ready_pods(1)
        with _channel(target, opts) as ch:  # ONE long-lived channel for the whole test
            _analyze(ch)
            if hpa:
                _kubectl("patch", hpa[0], "--type=merge", "-p", '{"spec":{"minReplicas":3}}')
            _kubectl("scale", dep, "--replicas=3")
            pods = _wait_ready_pods(3)
            base = {p: _requests_total(p) for p in pods}
            budget = int(MAX_CONN_AGE_S * 1.1) + GRACE_S + 60
            deadline, hit = time.monotonic() + budget, set[str]()
            while time.monotonic() < deadline and len(hit) < 2:
                _analyze(ch)
                time.sleep(1)
                hit = {p for p in pods if _requests_total(p) > base[p]}
            print(f"rebalance: pods_hit={sorted(hit)} of {pods} budget_s={budget}")
            assert len(hit) >= 2, f"client stayed pinned to {sorted(hit)} within {budget}s"
    finally:
        if hpa and hpa_min:
            _kubectl(
                "patch",
                hpa[0],
                "--type=merge",
                "-p",
                f'{{"spec":{{"minReplicas":{hpa_min}}}}}',
                check=False,
            )
        _kubectl("scale", dep, f"--replicas={original}", check=False)


def _wait_ready_pods(count: int, timeout_s: int = 300) -> list[str]:
    """Block until exactly ``count`` classification pods are Ready; FAIL on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pods = _pods()
        if len(pods) == count:
            return pods
        time.sleep(3)
    pytest.fail(f"did not reach {count} Ready classification pods within {timeout_s}s")
