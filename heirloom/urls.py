from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('genealogy/', include('genealogy.urls', namespace="genealogy")),
    path('records/', include('records.urls', namespace="records")),
    path('api/auth/', include('users.api.urls')),
    path('api/', include('genealogy.api.urls', namespace="api")),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )
