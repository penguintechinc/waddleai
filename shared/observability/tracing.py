"""OpenTelemetry bootstrap and tracer configuration.

Configures tracing from environment variables. If OTEL_EXPORTER_OTLP_ENDPOINT
is not set, tracing is a no-op and the app starts without error.

Semantic conventions (GenAI):
- gen_ai.system: LLM provider name
- gen_ai.request.model: Model requested by client
- gen_ai.response.model: Model returned by provider
- gen_ai.usage.input_tokens: Token count (input)
- gen_ai.usage.output_tokens: Token count (output)
- gen_ai.response.finish_reason: Completion reason
"""

import logging
import os
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

logger = logging.getLogger(__name__)

# Global tracer (initialized on first call)
_tracer: trace.Tracer | None = None
_initialized = False


@dataclass(slots=True)
class TracingConfig:
    """OpenTelemetry configuration from environment."""

    otlp_endpoint: str | None = None
    service_name: str = "waddleai"
    service_version: str = "unknown"
    deployment_environment: str = "development"
    traces_sampler: str = "always_on"

    @classmethod
    def from_env(cls) -> "TracingConfig":
        """Load configuration from environment variables."""
        # Read .version file for service version
        service_version = "unknown"
        version_file = ".version"
        if os.path.exists(version_file):
            try:
                with open(version_file) as f:
                    service_version = f.read().strip()
            except Exception as e:
                logger.warning(f"Failed to read .version file: {e}")

        return cls(
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
            service_name=os.getenv("OTEL_SERVICE_NAME", "waddleai"),
            service_version=service_version,
            deployment_environment=os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "development"),
            traces_sampler=os.getenv("OTEL_TRACES_SAMPLER", "always_on"),
        )


def init_tracing(config: TracingConfig | None = None) -> trace.Tracer:
    """Initialize OpenTelemetry tracing.

    If no OTLP endpoint is configured, returns a no-op tracer and the app
    continues without error. This is the fail-safe posture per spec §15.3.

    Args:
        config: TracingConfig; if None, loaded from environment

    Returns:
        Tracer instance (may be no-op if not configured)

    """
    global _tracer, _initialized

    if _initialized and _tracer is not None:
        return _tracer

    if config is None:
        config = TracingConfig.from_env()

    _initialized = True

    # If no endpoint, use no-op tracer
    if not config.otlp_endpoint:
        logger.debug("No OTEL_EXPORTER_OTLP_ENDPOINT configured; using no-op tracer")
        _tracer = trace.get_tracer("waddleai-noop")
        return _tracer

    # Create resource with service metadata
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": config.service_version,
            "deployment.environment": config.deployment_environment,
        }
    )

    try:
        # Create OTLP exporter
        otlp_exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint)

        # Create TracerProvider
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(SimpleSpanProcessor(otlp_exporter))

        # Set as global provider
        trace.set_tracer_provider(trace_provider)

        _tracer = trace.get_tracer("waddleai")
        logger.info(f"OpenTelemetry initialized: endpoint={config.otlp_endpoint}")
        return _tracer

    except Exception as e:
        logger.warning(f"Failed to initialize OpenTelemetry exporter: {e}; using no-op tracer")
        _tracer = trace.get_tracer("waddleai-noop")
        return _tracer


def get_tracer(service_name: str = "waddleai") -> trace.Tracer:
    """Get the global tracer instance.

    If not yet initialized, initializes from environment variables.
    If no OTLP endpoint is configured, returns a no-op tracer.

    Args:
        service_name: Service name for tracer (for logging; actual name from TracingConfig)

    Returns:
        Tracer instance

    """
    if _tracer is not None:
        return _tracer

    # Initialize on first call
    return init_tracing()
