import contextlib
import json
import logging
from typing import Optional, Tuple

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from mlflow.tracing.constant import SpanAttributeKey
from mlflow.utils.databricks_utils import is_in_databricks_model_serving_environment

# Once() object ensures a function is executed only once in a process.
# Note that it doesn't work as expected in a distributed environment.
_TRACER_PROVIDER_INITIALIZED = Once()

_logger = logging.getLogger(__name__)


def start_span_in_context(name: str) -> trace.Span:
    """
    Start a new OpenTelemetry span in the current context.

    Note that this function doesn't set the started span as the active span in the context. To do
    that, the upstream also need to call `use_span()` function in the OpenTelemetry trace APIs.

    Args:
        name: The name of the span.

    Returns:
        The newly created OpenTelemetry span.
    """
    return _get_tracer(__name__).start_span(name)


def start_detached_span(
    name: str, parent: Optional[trace.Span] = None, experiment_id: Optional[str] = None
) -> Optional[Tuple[str, trace.Span]]:
    """
    Start a new OpenTelemetry span that is not part of the current trace context, but with the
    explicit parent span ID if provided.

    Args:
        name: The name of the span.
        parent: The parent OpenTelemetry span. If not provided, the span will be created as a root
                span.
        experiment_id: The ID of the experiment. This is used to associate the span with a specific
            experiment in MLflow.

    Returns:
        The newly created OpenTelemetry span.
    """
    tracer = _get_tracer(__name__)
    context = trace.set_span_in_context(parent) if parent else None
    attributes = (
        {SpanAttributeKey.EXPERIMENT_ID: json.dumps(experiment_id)} if experiment_id else None
    )
    return tracer.start_span(name, context=context, attributes=attributes)


def _get_tracer(module_name: str):
    """
    Get a tracer instance for the given module name.
    """
    # Initiate tracer provider only once in the application lifecycle
    _TRACER_PROVIDER_INITIALIZED.do_once(_setup_tracer_provider)

    tracer_provider = trace.get_tracer_provider()
    return tracer_provider.get_tracer(module_name)


def _setup_tracer_provider(disabled=False):
    """
    Instantiate a tracer provider and set it as the global tracer provider.
    """
    if disabled:
        _force_set_otel_tracer_provider(trace.NoOpTracerProvider())
        return

    if is_in_databricks_model_serving_environment():
        from mlflow.tracing.export.inference_table import InferenceTableSpanExporter
        from mlflow.tracing.processor.inference_table import InferenceTableSpanProcessor

        exporter = InferenceTableSpanExporter()
        processor = InferenceTableSpanProcessor(exporter)
    else:
        from mlflow.tracing.export.mlflow import MlflowSpanExporter
        from mlflow.tracing.processor.mlflow import MlflowSpanProcessor

        exporter = MlflowSpanExporter()
        processor = MlflowSpanProcessor(exporter)

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(processor)
    _force_set_otel_tracer_provider(tracer_provider)


def _force_set_otel_tracer_provider(tracer_provider):
    """
    Resetting internal flag used in OpenTelemetry. If we don't reset the flag,
    set_tracer_provider() will be a no-op after the first call
    in the application lifecycle.
    https://github.com/open-telemetry/opentelemetry-python/blob/v1.24.0/opentelemetry-api/src/opentelemetry/trace/__init__.py#L485
    """
    with trace._TRACER_PROVIDER_SET_ONCE._lock:
        trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace.set_tracer_provider(tracer_provider)


def disable():
    """
    Disable tracing by setting the global tracer provider to NoOpTracerProvider.
    """
    if not _is_enabled():
        return

    reset_tracer_setup()  # Force re-initialization of the tracer provider
    _TRACER_PROVIDER_INITIALIZED.do_once(lambda: _setup_tracer_provider(disabled=True))


def enable():
    """
    Enable tracing by setting the global tracer provider to the actual tracer provider.
    """
    if _is_enabled():
        _logger.info("Tracing is already enabled")
        return

    _setup_tracer_provider()


@contextlib.contextmanager
def trace_disabled():
    """
    Temporarily disable tracing for the duration of the context manager.

    :meta private:
    """
    was_trace_enabled = _is_enabled()
    try:
        disable()
        yield
    finally:
        if was_trace_enabled:
            enable()


def reset_tracer_setup():
    """
    Reset the flags that indicates whether the tracer provider has been initialized.
    This ensures that the tracer provider is re-initialized when next tracing
    operation is performed.
    """
    with _TRACER_PROVIDER_INITIALIZED._lock:
        _TRACER_PROVIDER_INITIALIZED._done = False
    # Set NoOp tracer provider to reset the global tracer to the initial state.
    # Do not flip _TRACE_PROVIDER_INITIALIZED flag to True so that
    # the next tracing operation will re-initialize the provider.
    _setup_tracer_provider(disabled=True)


def _is_enabled() -> bool:
    """
    Check if tracing is enabled based on whether the global tracer
    is instantiated or not.

    Trace is considered as "enabled" if the followings
    1. The default state (before any tracing operation)
    2. The tracer is not either ProxyTracer or NoOpTracer
    """
    with _TRACER_PROVIDER_INITIALIZED._lock:
        tracer = trace.get_tracer(__name__)

        # Occasionally ProxyTracer instance wraps the actual tracer
        if isinstance(tracer, trace.ProxyTracer):
            tracer = tracer._tracer

        return not (_TRACER_PROVIDER_INITIALIZED._done and isinstance(tracer, trace.NoOpTracer))
