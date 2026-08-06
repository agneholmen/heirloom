from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail

User = get_user_model()

class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254, trim_whitespace=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RegistrationSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password2 = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_username(self, value):
        value = value.strip()
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                ErrorDetail("This username is already in use.", code="username_already_registered")
            )
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                ErrorDetail("This email address is already in use.", code="email_already_registered")
            )
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password2": "The passwords do not match."})
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class TokenSerializer(serializers.Serializer):
    uid = serializers.CharField(trim_whitespace=True)
    token = serializers.CharField(trim_whitespace=True)


class PasswordResetRequestSerializer(serializers.Serializer):
    # Deliberately permissive so unknown/malformed addresses get the same response.
    email = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email(self, value):
        return value.strip().lower()


class PasswordResetConfirmSerializer(TokenSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password2 = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password2": "The passwords do not match."})
        return attrs

    def validate_password_for_user(self, user):
        try:
            validate_password(self.validated_data["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

class UserProfileSerializer(serializers.ModelSerializer):
    """Serializer for user profile data"""
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'first_name',
            'last_name',
            'email',
            'date_of_birth',
            'description',
            'sex',
            'photo',
            'photo_url'
        ]
        read_only_fields = ['id', 'username']

    def get_photo_url(self, obj):
        if obj.photo:
            return obj.photo.url
        return None
