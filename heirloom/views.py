import json

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST


def _user_data(user):
    return {
        "id": user.pk,
        "pk": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


@require_GET
@ensure_csrf_cookie
def csrf_cookie(request):
    """Set the CSRF cookie used by browser clients for unsafe API requests."""
    return JsonResponse({"detail": "CSRF cookie set"})


@require_POST
@csrf_protect
def session_login(request):
    try:
        credentials = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"non_field_errors": ["Invalid JSON request."]},
            status=400,
        )

    if not isinstance(credentials, dict):
        return JsonResponse(
            {"non_field_errors": ["Expected a JSON object."]},
            status=400,
        )

    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return JsonResponse(
            {"non_field_errors": ["Username and password are required."]},
            status=400,
        )

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse(
            {"non_field_errors": ["Unable to log in with provided credentials."]},
            status=400,
        )

    login(request, user)
    return JsonResponse(_user_data(user))


@require_POST
@csrf_protect
def session_logout(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Authentication required."}, status=401)

    logout(request)
    return JsonResponse({"detail": "Successfully logged out."})


@require_GET
def current_user(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Authentication required."}, status=401)

    return JsonResponse(_user_data(request.user))
