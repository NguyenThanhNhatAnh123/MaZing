"""Request-independent device context, explicitly carried into worker threads."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps

WORKSPACE_ID = ContextVar("mayzing_workspace", default="")
RUNTIME = ContextVar("mayzing_runtime", default=None)
BRIDGE_GENERATION = ContextVar("mayzing_bridge_generation", default=None)


def bind_context(function):
    """Capture only our device context, never a Flask request or its cookies."""
    workspace, runtime = WORKSPACE_ID.get(), RUNTIME.get()
    generation = BRIDGE_GENERATION.get()

    @wraps(function)
    def run(*args, **kwargs):
        workspace_token = WORKSPACE_ID.set(workspace)
        runtime_token = RUNTIME.set(runtime)
        generation_token = BRIDGE_GENERATION.set(generation)
        try:
            return function(*args, **kwargs)
        finally:
            BRIDGE_GENERATION.reset(generation_token)
            RUNTIME.reset(runtime_token)
            WORKSPACE_ID.reset(workspace_token)
    return run
