from __future__ import annotations

from collections.abc import Mapping, Sequence

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.exceptions import ErrorDetail
from rest_framework.views import exception_handler as drf_exception_handler


DEFAULT_MESSAGES = {
    "authentication_required": "Authentication is required.",
    "invalid_credentials": "Username or password is incorrect.",
    "account_inactive": "This account is inactive.",
    "permission_denied": "You do not have permission to perform this action.",
    "not_found": "The requested resource was not found.",
    "csrf_failed": "CSRF validation failed.",
    "validation_error": "One or more value is not valid.",
    "conflict": "The request is in conflict with the current status.",
    "rate_limited": "Too many requests. Try again later.",
}


class APIError(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "validation_error"

    def __init__(self, code: str, message: str | None = None, *, status_code: int | None = None):
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail=message or DEFAULT_MESSAGES.get(code, "Request failed."), code=code)


def _error_item(value) -> dict[str, str]:
    if isinstance(value, ErrorDetail):
        return {"code": value.code, "message": str(value)}
    return {"code": "invalid", "message": str(value)}


def _field_errors(detail) -> dict[str, list[dict[str, str]]]:
    if not isinstance(detail, Mapping):
        return {}

    fields: dict[str, list[dict[str, str]]] = {}
    for field, value in detail.items():
        if field in {"detail", "non_field_errors"}:
            continue
        if isinstance(value, Mapping):
            fields[field] = [_error_item(value)]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            fields[field] = [_error_item(item) for item in value]
        else:
            fields[field] = [_error_item(value)]
    return fields


def _first_detail(detail, fallback: str) -> str:
    if isinstance(detail, Mapping):
        non_field = detail.get("non_field_errors") or detail.get("detail")
        if isinstance(non_field, Sequence) and not isinstance(non_field, (str, bytes)) and non_field:
            return str(non_field[0])
        if non_field:
            return str(non_field)
        return fallback
    if isinstance(detail, Sequence) and not isinstance(detail, (str, bytes)) and detail:
        return str(detail[0])
    return str(detail) if detail else fallback


def _exception_code(exc, response) -> str:
    if isinstance(exc, APIError):
        return exc.get_codes()
    if isinstance(exc, exceptions.ValidationError):
        return "validation_error"
    if isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        return "authentication_required"
    if isinstance(exc, exceptions.Throttled):
        return "rate_limited"
    if isinstance(exc, (exceptions.NotFound, Http404)):
        return "not_found"
    if isinstance(exc, (exceptions.PermissionDenied, DjangoPermissionDenied)):
        detail = str(getattr(exc, "detail", exc))
        return "csrf_failed" if "CSRF" in detail else "permission_denied"
    if response.status_code == status.HTTP_404_NOT_FOUND:
        return "not_found"
    if response.status_code == status.HTTP_403_FORBIDDEN:
        return "permission_denied"
    return "validation_error"


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    code = _exception_code(exc, response)
    message = _first_detail(detail, DEFAULT_MESSAGES.get(code, "Request failed."))
    payload = {
        "code": code,
        "message": message,
    }

    fields = _field_errors(detail)
    if fields:
        payload["fields"] = fields

    response.data = {"error": payload}
    return response
