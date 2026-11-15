"""URL configuration for the football project.

All API routes live in ``api/urls.py`` and are mounted under ``/api/``.
There is deliberately no ``api/v1/`` package: the api app stays flat.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
]