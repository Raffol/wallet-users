from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Студенческий МедиаЦентр"
admin.site.site_title = "СМЦ — управление"
admin.site.index_title = "Управление содержимым"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls_auth")),
    path("api/tags/", include("accounts.urls_tags")),
    path("api/members/", include("accounts.urls_members")),
    path("api/requests/", include("tickets.urls")),
    path("api/posts/", include("posts.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
