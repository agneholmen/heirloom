from django.urls import path
from .views import (
    csrf_token_view,
    LoginView, 
    LogoutView, 
    MeView,
    RegisterView,
    VerifyEmailView,
    PasswordResetRequestView,
    PasswordResetValidateView,
    PasswordResetConfirmView,
)

urlpatterns = [
    path("csrf", csrf_token_view, name="auth_csrf"),
    path("login", LoginView.as_view(), name="auth_login"),
    path("logout", LogoutView.as_view(), name="auth_logout"),
    path("me", MeView.as_view(), name="auth_me"),
    path("register", RegisterView.as_view(), name="auth_register"),
    path("verify-email", VerifyEmailView.as_view(), name="auth_verify_email"),
    path("password-reset-request", PasswordResetRequestView.as_view(), name="auth_password_reset_request"),
    path("password-reset-validate", PasswordResetValidateView.as_view(), name="auth_password_reset_validate"),
    path("password-reset-confirm", PasswordResetConfirmView.as_view(), name="auth_password_reset_confirm"),
]
