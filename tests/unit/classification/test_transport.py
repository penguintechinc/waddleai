"""Transport-security tests: TLS-by-default, fail-closed SPIFFE, real mTLS, bind-before-warm."""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import auth_fakes as af
import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from services.classification import app as app_pkg
from services.classification.app import engine as eng
from services.classification.app.auth import (
    OUTAGE_WARN_INTERVAL_SECONDS,
    LastKnownKeyResolver,
)
from services.classification.app.config import ClassificationConfig, ConfigError
from services.classification.app.grpc_gen import classification_pb2 as pb
from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc
from services.classification.app.grpc_server import (
    bound_port,
    build_grpc_server,
    load_server_credentials,
)
from services.classification.app.lifecycle import startup
from services.classification.app.servicer import ClassificationServicer

SPIFFE = "spiffe://penguintech.io/test/proxy"


def _req() -> Any:
    """Valid Analyze request."""
    return pb.AnalyzeRequest(api_version="v1", text="Alice here", language="en", org_public_id="o")


def _servicer(engine: Any, cfg: ClassificationConfig) -> ClassificationServicer:
    """Real servicer over doubles."""
    return ClassificationServicer(
        engine, af.FakeCache(), af.FakeGate(), af.FakePolicy(), cfg.max_text_bytes
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # no TLS, no dev opt-in
        {"tls_cert_file": "c"},  # half pair
        {"tls_key_file": "k", "allow_insecure_grpc": True},  # half pair even in dev
        {"spiffe_allowed_ids": frozenset({SPIFFE}), "allow_insecure_grpc": True},
        {
            "spiffe_allowed_ids": frozenset({SPIFFE}),
            "tls_cert_file": "c",
            "tls_key_file": "k",
        },  # TLS but no client CA
        {"spiffe_allowed_ids": frozenset({"https://x"}), "allow_insecure_grpc": True},
    ],
)
def test_validate_transport_refuses(kwargs: dict[str, Any]) -> None:
    """Every unsafe transport configuration is refused at validation."""
    with pytest.raises(ConfigError):
        ClassificationConfig(**kwargs).validate_transport()


def test_validate_transport_accepts_tls_mtls_and_explicit_dev() -> None:
    """TLS, mTLS+SPIFFE and the explicit dev opt-in are all accepted."""
    ClassificationConfig(tls_cert_file="c", tls_key_file="k").validate_transport()
    ClassificationConfig(
        tls_cert_file="c",
        tls_key_file="k",
        tls_client_ca_file="ca",
        spiffe_allowed_ids=frozenset({SPIFFE}),
    ).validate_transport()
    ClassificationConfig(allow_insecure_grpc=True).validate_transport()


def test_transport_env_parsing() -> None:
    """TLS paths and the dev flag come from env; the dev flag is strictly '1'."""
    cfg = ClassificationConfig.from_env(
        {
            "GRPC_TLS_CERT_FILE": " /c ",
            "GRPC_TLS_KEY_FILE": "/k",
            "GRPC_TLS_CLIENT_CA_FILE": "/ca",
            "ALLOW_INSECURE_GRPC": "1",
        }
    )
    assert (cfg.tls_cert_file, cfg.tls_key_file, cfg.tls_client_ca_file) == ("/c", "/k", "/ca")
    assert cfg.allow_insecure_grpc is True
    assert (
        ClassificationConfig.from_env({"ALLOW_INSECURE_GRPC": "true"}).allow_insecure_grpc is False
    )


async def test_startup_refuses_cleartext_without_dev_optin() -> None:
    """Default config (no TLS, no opt-in) refuses to start."""
    app = app_pkg.create_app(ClassificationConfig(grpc_port=0), engine=af.FakeEngine())
    with pytest.raises(ConfigError, match="TLS is required"):
        await startup(app)


async def test_startup_refuses_spiffe_without_tls() -> None:
    """SPIFFE_ALLOWED_IDS without mTLS material is refused, even with the dev opt-in."""
    cfg = ClassificationConfig(
        grpc_port=0, spiffe_allowed_ids=frozenset({SPIFFE}), allow_insecure_grpc=True
    )
    app = app_pkg.create_app(cfg, engine=af.FakeEngine())
    with pytest.raises(ConfigError, match="SPIFFE_ALLOWED_IDS"):
        await startup(app)


def test_last_known_warning_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JWKS-outage warning fires once per interval, not once per RPC."""
    from jwt import PyJWKClientConnectionError

    import services.classification.app.auth as auth_mod

    warnings: list[str] = []

    class _Log:
        def warning(self, event: str, **_: Any) -> None:
            warnings.append(event)

    monkeypatch.setattr(auth_mod, "log", _Log())
    clock = [0.0]
    inner = af.StaticResolver()
    res = LastKnownKeyResolver(inner, clock=lambda: clock[0])
    token = af.make_token()
    res.get_signing_key_from_jwt(token)
    inner.fail_with = PyJWKClientConnectionError("down")
    for _ in range(5):
        res.get_signing_key_from_jwt(token)
    assert len(warnings) == 1
    clock[0] = OUTAGE_WARN_INTERVAL_SECONDS + 1
    res.get_signing_key_from_jwt(token)
    assert len(warnings) == 2


class _NotReadyEngine(af.FakeEngine):
    """Engine that has not warmed: analyze raises EngineUnavailable like the real one."""

    async def analyze(self, text: str, language: str, deadline_s: float) -> list[Any]:
        """Refuse like a cold NerEngine."""
        raise eng.EngineUnavailable("NER engine is not ready")


async def test_rpc_before_engine_ready_is_clean_backend_unavailable() -> None:
    """Bind-before-warm: an authenticated RPC to a cold engine degrades cleanly (no crash)."""
    cfg = ClassificationConfig(grpc_port=0, allow_insecure_grpc=True)
    engine = _NotReadyEngine(ready=False)
    server = await build_grpc_server(cfg, _servicer(engine, cfg), af.make_verifier())
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}") as ch:
        stub = pb_grpc.ClassificationServiceStub(ch)
        resp = await stub.Analyze(
            _req(), metadata=(("authorization", f"Bearer {af.make_token()}"),)
        )
    await server.stop(0)
    assert resp.ner_status == pb.NER_STATUS_BACKEND_UNAVAILABLE
    assert resp.degraded is True
    assert resp.degraded_reason == pb.DEGRADED_REASON_BACKEND_UNAVAILABLE
    assert len(resp.spans) == 0


def _key() -> rsa.RSAPrivateKey:
    """Fresh throwaway RSA key (generated at test time, never committed)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(
    subject: str,
    key: rsa.RSAPrivateKey,
    issuer_key: rsa.RSAPrivateKey,
    issuer_name: x509.Name,
    sans: list[x509.GeneralName],
    *,
    ca: bool = False,
) -> x509.Certificate:
    """Issue a short-lived self-signed-chain test certificate."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> bytes:
    """Encode a certificate as PEM."""
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    """Encode a private key as PEM (test material only)."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


async def test_real_mtls_spiffe_over_tls_channel(tmp_path: Path) -> None:
    """Real mTLS: allow-listed SPIFFE URI SAN passes with no token; others are rejected.

    Uses the real gRPC auth_context() (no fake context) through the full app lifecycle.
    """
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = _cert("test-ca", ca_key, ca_key, ca_name, [], ca=True)
    srv_key = _key()
    srv_cert = _cert(
        "server",
        srv_key,
        ca_key,
        ca_name,
        [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
    )
    paths = {
        "ca": tmp_path / "ca.pem",
        "cert": tmp_path / "srv.pem",
        "key": tmp_path / "srv.key",
    }
    paths["ca"].write_bytes(_pem(ca_cert))
    paths["cert"].write_bytes(_pem(srv_cert))
    paths["key"].write_bytes(_key_pem(srv_key))

    def client_creds(sans: list[x509.GeneralName] | None) -> grpc.ChannelCredentials:
        """Client credentials with the given SANs (None = no client cert at all)."""
        if sans is None:
            return grpc.ssl_channel_credentials(root_certificates=_pem(ca_cert))
        key = _key()
        cert = _cert("client", key, ca_key, ca_name, sans)
        return grpc.ssl_channel_credentials(_pem(ca_cert), _key_pem(key), _pem(cert))

    cfg = ClassificationConfig(
        grpc_port=0,
        tls_cert_file=str(paths["cert"]),
        tls_key_file=str(paths["key"]),
        tls_client_ca_file=str(paths["ca"]),
        spiffe_allowed_ids=frozenset({SPIFFE}),
    )
    assert load_server_credentials(ClassificationConfig()) is None
    engine = af.FakeEngine()
    app = app_pkg.create_app(
        cfg, engine=engine, servicer=_servicer(engine, cfg), verifier=af.make_verifier()
    )
    opts = (("grpc.ssl_target_name_override", "localhost"),)

    async with app.test_app():
        port = bound_port(app.extensions["classification_runtime"].server)
        target = f"127.0.0.1:{port}"

        async def call(creds: grpc.ChannelCredentials, token: str | None = None) -> Any:
            async with grpc.aio.secure_channel(target, creds, options=opts) as ch:
                md = (("authorization", f"Bearer {token}"),) if token else None
                return await pb_grpc.ClassificationServiceStub(ch).Analyze(
                    _req(), metadata=md, timeout=5
                )

        ok = await call(client_creds([x509.UniformResourceIdentifier(SPIFFE)]))
        assert ok.ner_status == pb.NER_STATUS_OK

        for sans in (
            [x509.UniformResourceIdentifier("spiffe://penguintech.io/test/other")],
            [x509.DNSName("spiffe.example")],
            [x509.UniformResourceIdentifier("https://penguintech.io/test/proxy")],
        ):
            with pytest.raises(grpc.aio.AioRpcError) as ei:
                await call(client_creds(sans))
            assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED

        # mTLS is mandatory: no client certificate means the handshake fails outright.
        with pytest.raises(grpc.aio.AioRpcError) as ei:
            await call(client_creds(None))
        assert ei.value.code() == grpc.StatusCode.UNAVAILABLE

        # A non-allow-listed mTLS peer can still authenticate with a valid JWT.
        via_jwt = await call(client_creds([x509.DNSName("other")]), af.make_token())
        assert via_jwt.ner_status == pb.NER_STATUS_OK
    await asyncio.sleep(0)
