from django.urls import path
from . import views


urlpatterns = [

    # Movie List
    path(
        'movies/',
        views.movie_list,
        name='movie_list'
    ),

    # Movie Details
    path(
        'movie/<int:pk>/',
        views.movie_detail,
        name='movie_detail'
    ),

    # Theater List
    path(
        'theaters/<int:movie_id>/',
        views.theater_list,
        name='theater_list'
    ),

    # Book Seats
    path(
        'book-seats/<int:theater_id>/',
        views.book_seats,
        name='book_seats'
    ),

    # Reserve Seat
    path(
        'reserve-seat/<int:seat_id>/',
        views.reserve_seat_view,
        name='reserve_seat'
    ),

    # Payment Success
    path(
        'payment-success/',
        views.payment_success,
        name='payment_success'
    ),

    # Payment Failed
    path(
        'payment-failed/',
        views.payment_failed,
        name='payment_failed'
    ),

    # Razorpay Webhook
    path(
        'razorpay/webhook/',
        views.razorpay_webhook,
        name='razorpay_webhook'
    ),

    # Admin Dashboard
    path(
        'admin-dashboard/',
        views.admin_dashboard,
        name='admin_dashboard'
    ),

    # Admin Analytics API
    path(
        'admin-dashboard/api/',
        views.admin_analytics_api,
        name='admin_analytics_api'
    ),
]