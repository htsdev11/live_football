"""Uniform error envelope: ``{code, message, details, request_id}``."""

from __future__ import annotations

import logging
import uuid

from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger('api')

#: HTTP status -> stable machine-readable code.
ERROR_CODES = {
    400: 'validation_error',
    401: 'authentication_required',
    403: 'permission_denied',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    429: 'rate_limited',
    500: 'internal_error',
    503: 'service_unavailable',
}


def request_id_for(request) -> str:
    """Return a stable correlation ID for this request."""
    if request is None:
        return uuid.uuid4().hex
    existing = getattr(request, '_api_request_id', None)
    if existing:
        return existing
    header = None
    meta = getattr(request, 'META', None) or {}
    header = meta.get('HTTP_X_REQUEST_ID')
    value = header or uuid.uuid4().hex
    try:
        request._api_request_id = value
    except AttributeError:  # pragma: no cover - immutable request objects
        pass
    return value


def error_payload(code: str, message: str, details=None, request_id: str = '') -> dict:
    return {
        'code': code,
        'message': message,
        'details': details if details is not None else {},
        'request_id': request_id,
    }


def api_exception_handler(exc, context):
    """DRF exception handler producing the documented error schema."""
    response = drf_exception_handler(exc, context)
    request = context.get('request')
    request_id = request_id_for(request)

    if response is None:
        logger.exception('Unhandled API error (request_id=%s)', request_id)
        return Response(
            error_payload(
                ERROR_CODES[500],
                'An unexpected server error occurred.',
                request_id=request_id,
            ),
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code = ERROR_CODES.get(response.status_code, 'error')
    data = response.data
    message = 'Request failed.'
    details: object = {}

    if isinstance(data, dict):
        detail = data.get('detail')
        if detail is not None and len(data) == 1:
            message = str(detail)
        else:
            message = 'The request could not be validated.'
            details = data
    elif isinstance(data, list):
        message = 'The request could not be validated.'
        details = data

    response.data = error_payload(code, message, details, request_id)
    if response.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS:
        wait = getattr(exc, 'wait', None)
        if wait:
            response['Retry-After'] = str(int(wait))
    return response
