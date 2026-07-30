from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from .views import csrf_cookie, current_user, session_login, session_logout

urlpatterns = [
    path('admin/', admin.site.urls),
    path('genealogy/', include('genealogy.urls', namespace="genealogy")),
    path('users/', include('users.urls', namespace="users")),
    path('records/', include('records.urls', namespace="records")),
    path('api/csrf/', csrf_cookie, name='csrf-cookie'),
    path('api/auth/login/', session_login, name='session-login'),
    path('api/auth/logout/', session_logout, name='session-logout'),
    path('api/auth/user/', current_user, name='current-user'),
    path('api/', include('genealogy.api.urls', namespace="api")),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )
