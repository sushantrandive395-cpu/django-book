
import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Booking, Seat


logger = logging.getLogger("movies")


# =========================================================
# BOOKING CONFIRMATION EMAIL
# =========================================================

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_booking_email(self, booking_id):

    try:

        booking = Booking.objects.select_related(
            "user",
            "movie",
            "theater",
            "seat",
            "payment",
        ).get(id=booking_id)

        payment = booking.payment

        html_message = render_to_string(
            "emails/booking_confirmation.html",
            {
                "user_name": booking.user.username,
                "movie_name": booking.movie.name,
                "theater_name": booking.theater.name,
                "show_time": booking.theater.time,
                "seat_number": booking.seat.seat_number,
                "booking_id": booking.id,
                "payment_id": payment.payment_id,
                "amount": payment.amount,
                "payment_method": payment.payment_method,
                "payment_status": payment.payment_status,
            },
        )

        email = EmailMultiAlternatives(
            subject="BookMySeat - Booking Confirmation",
            body="Your booking has been confirmed.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[booking.user.email],
        )

        email.attach_alternative(
            html_message,
            "text/html",
        )

        email.send(fail_silently=False)

        logger.info(
            "Booking email sent successfully. Booking ID=%s",
            booking_id,
        )

        return True

    except Exception:

        logger.exception(
            "Booking email failed. Booking ID=%s",
            booking_id,
        )

        raise


# =========================================================
# AUTO RELEASE EXPIRED SEAT RESERVATIONS
# =========================================================

@shared_task
def release_expired_seat_reservations():

    now = timezone.now()

    expired_seats = Seat.objects.filter(
        reserved_until__isnull=False,
        reserved_until__lte=now,
        is_booked=False,
    )

    count = expired_seats.update(
        reserved_by=None,
        reserved_until=None,
    )

    logger.info(
        "Released %s expired seat reservation(s).",
        count,
    )

    print(
        f"Released {count} expired seat reservation(s)."
    )

    return count