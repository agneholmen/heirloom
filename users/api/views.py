from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import Throttled
from rest_framework.throttling import AnonRateThrottle
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import ensure_csrf_cookie

from users.api.serializers import (
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegistrationSerializer,
    TokenSerializer,
    UserProfileSerializer,
)

from heirloom.api_errors import APIError

class LoginRateThrottle(AnonRateThrottle):
    scope = 'login'

class RegisterRateThrottle(AnonRateThrottle):
    scope = 'register'

class PasswordResetRateThrottle(AnonRateThrottle):
    scope = 'password_reset'

User = get_user_model()

@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf_token_view(request: HttpRequest) -> Response:
    return Response({"detail": "CSRF cookie set.", "csrfToken": get_token(request)}, status=status.HTTP_200_OK)

class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def throttled(self, request, wait):
        raise Throttled(detail=f"Too many login attempts. Try again in {int(wait)} seconds.")

    def post(self, request):
        # SessionAuthentication checks CSRF only after an existing session
        # authenticates a request. Login starts anonymous, so enforce it here.
        SessionAuthentication().enforce_csrf(request)

        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]
        user = authenticate(request=request, username=username, password=password)

        if not user:
            # Check if this isn't working because of inactive account
            user_obj = User.objects.filter(username=username).first()
            if not user_obj:
                # Optional: Handle email fallback if login accepts emails
                user_obj = User.objects.filter(email__iexact=username).first()
                
            if user_obj and not user_obj.is_active and user_obj.check_password(password):
                raise APIError(
                    "account_inactive",
                    "Your account has not been verified. Check your email for the verification link.",
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            raise APIError(
                "invalid_credentials",
                "Invalid credentials.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        login(request, user)

        return Response({
            # django.contrib.auth.login rotates the CSRF secret. Return the
            # replacement because the shop cannot read admin's host-only cookie.
            "csrfToken": get_token(request),
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        })

class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            logout(request)

        return Response({"detail": "Logged out"})


class MeView(APIView):
    def get(self, request):
        return Response(UserProfileSerializer(request.user).data)

    def patch(self, request):
        serializer = UserProfileSerializer(
            request.user,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [RegisterRateThrottle]

    @transaction.atomic
    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = User.objects.create_user(
            username=data["username"], email=data["email"],
            first_name=data["first_name"], last_name=data["last_name"],
            password=data["password"], is_active=False,
        )
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/verify-email?uid={uid}&token={token}"
        send_mail(
            "Verify your Project Heirloom account",
            f"Hello {user.first_name},\n\nVerify your account using this link:\n{link}\n\nIf you did not create this account, you can ignore this message.",
            settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False,
        )
        return Response(
            {"detail": "Account created. Check your email for the verification link."},
            status=status.HTTP_201_CREATED,
        )


def _user_from_uid(uidb64):
    try:
        return User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _user_from_uid(serializer.validated_data["uid"])
        if user is None or not default_token_generator.check_token(user, serializer.validated_data["token"]):
            raise APIError("invalid_verification_token", "The verification link is invalid or has expired.")
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        return Response({"detail": "Your email address has been verified. You can now sign in."})


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data["email"], is_active=True).first()
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/reset-password?uid={uid}&token={token}"
            send_mail(
                "Reset your Project Heirloom password",
                f"Hello {user.first_name},\n\nCreate a new password using this link:\n{link}\n\nIf you did not request this, you can ignore this message.",
                settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False,
            )
        return Response({"detail": "If an active account uses that email address, a password reset link has been sent."})


class PasswordResetValidateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _user_from_uid(serializer.validated_data["uid"])
        if user is None or not default_token_generator.check_token(user, serializer.validated_data["token"]):
            raise APIError("invalid_reset_token", "The password reset link is invalid or has expired.")
        return Response({"detail": "The password reset link is valid."})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _user_from_uid(serializer.validated_data["uid"])
        if user is None or not default_token_generator.check_token(user, serializer.validated_data["token"]):
            raise APIError("invalid_reset_token", "The password reset link is invalid or has expired.")
        serializer.validate_password_for_user(user)
        user.set_password(serializer.validated_data["password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Your password has been updated. You can now sign in."})
