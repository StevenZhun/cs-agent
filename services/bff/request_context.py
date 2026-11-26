"""Request and trace identity propagation for the BFF boundary."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import FastAPI, Request

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"


def _accepted_or_new(value: str | None, prefix: str) -> str:
    if value is not None and ID_PATTERN.fullmatch(value):
        return value
    return f"{prefix}_{uuid4().hex}"


def install_request_context(app: FastAPI) -> None:
    """Create or propagate safe IDs and echo them on every response."""

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = _accepted_or_new(request.headers.get(REQUEST_ID_HEADER), "req")
        trace_id = _accepted_or_new(request.headers.get(TRACE_ID_HEADER), "trace")
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id
        return response
