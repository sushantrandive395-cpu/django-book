from django.urls import path
from .views import register, login_view, profile, reset_password, home
from django.contrib.auth import views as auth_views
from . import views


urlpatterns = [
    # Home
    path('', home, name='home'),

    # Register
    path('register/', register, name='register'),

    # Login
    path('login/', login_view, name='login'),

    # Profile
    path('profile/', profile, name='profile'),

    # Change Password
    path('reset-password/', reset_password, name='reset-password'),

    # Logout
    path(
        'logout/',
        auth_views.LogoutView.as_view(
            template_name='logout.html'
        ),
        name='logout'
    ),

    # Password Reset
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='reset_password.html'
        ),
        name='password_reset'
    ),

    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='password_reset_done.html'
        ),
        name='password_reset_done'
    ),

    path(
        'password-reset-confirm/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='password_reset_confirm.html'
        ),
        name='password_reset_confirm'
    ),

    path(
        'password-reset-complete/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='password_reset_complete.html'
        ),
        name='password_reset_complete'
    ),

    # Messages
    path('messages/', views.messages, name='messages'),
]