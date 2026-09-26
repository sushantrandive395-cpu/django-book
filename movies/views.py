from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import IntegrityError, transaction
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.urls import reverse
from django.core.paginator import Paginator
from django.core.cache import cache

from .analytics import get_admin_analytics

from decimal import Decimal
from datetime import timedelta

import uuid
import razorpay
import hmac
import hashlib
import json
def get_youtube_id(url):
    """
    Safely extract YouTube video ID.
    Supports:
    https://www.youtube.com/watch?v=VIDEO_ID
    https://youtu.be/VIDEO_ID
    """

    if not url:
        return None

    try:
        parsed = urlparse(url.strip())

        # Only allow HTTPS
        if parsed.scheme != "https":
            return None

        domain = parsed.netloc.lower()

        # youtube.com
        if domain in ["www.youtube.com", "youtube.com"]:
            if parsed.path == "/watch":
                video_id = parse_qs(
                    parsed.query
                ).get("v", [None])[0]

                if video_id and len(video_id) == 11:
                    return video_id

        # youtu.be
        elif domain in ["youtu.be", "www.youtu.be"]:
            video_id = parsed.path.strip("/")

            if video_id:
                video_id = video_id.split("/")[0]

                if len(video_id) == 11:
                    return video_id

    except Exception:
        return None

    return None

from .models import (
    Movie,
    Theater,
    Seat,
    Booking,
    Genre,
    Language,
    Payment,
    RazorpayWebhookEvent,
)

from .tasks import send_booking_email
from .utils import get_youtube_id

from .services import (
    reserve_seat,
    SeatReservationError,
)


# ============================================================
# RAZORPAY CLIENT
# ============================================================

client = razorpay.Client(
    auth=(
        settings.RAZORPAY_KEY_ID,
        settings.RAZORPAY_KEY_SECRET,
    )
)


# ============================================================
# PAYMENT CONFIGURATION
# ============================================================

PAYMENT_AMOUNT = Decimal("200.00")

PAYMENT_TIMEOUT_MINUTES = 10

SEAT_RESERVATION_MINUTES = 2


# ============================================================
# PAYMENT METHOD MAP
# ============================================================

PAYMENT_METHOD_MAP = {
    "card": "CARD",
    "upi": "UPI",
    "netbanking": "NETBANKING",
    "net_banking": "NETBANKING",
    "wallet": "WALLET",
    "emi": "EMI",
    "paylater": "PAYLATER",
    "pay_later": "PAYLATER",
}


# ============================================================
# NORMALIZE PAYMENT METHOD
# ============================================================

def normalize_payment_method(method):

    if not method:
        return None

    method = str(method).strip().lower()

    return PAYMENT_METHOD_MAP.get(
        method,
        "OTHER",
    )


# ============================================================
# CHECK PAYMENT TIMEOUT
# ============================================================

def is_payment_expired(payment):

    if payment.payment_status != "PENDING":
        return False

    expiry_time = (
        payment.created_at
        + timedelta(
            minutes=PAYMENT_TIMEOUT_MINUTES
        )
    )

    return timezone.now() >= expiry_time


# ============================================================
# EXPIRE PAYMENT
# ============================================================

def expire_payment(payment):

    if payment.payment_status != "PENDING":
        return False

    booking = payment.booking
    seat = booking.seat

    payment.payment_status = "TIMEOUT"

    payment.save(
        update_fields=[
            "payment_status",
            "updated_at",
        ]
    )

    if booking.booking_status == "PENDING":

        booking.booking_status = "EXPIRED"

        booking.save(
            update_fields=[
                "booking_status",
            ]
        )

    seat.is_booked = False
    seat.reserved_by = None
    seat.reserved_until = None

    seat.save(
        update_fields=[
            "is_booked",
            "reserved_by",
            "reserved_until",
        ]
    )

    return True


# ============================================================
# MOVIE DETAIL
# ============================================================

def movie_detail(request, pk):

    movie = get_object_or_404(
        Movie,
        id=pk,
    )

    video_id = None

    if movie.trailer_url:

        video_id = get_youtube_id(
            movie.trailer_url
        )

    return render(
        request,
        "movies/movie_detail.html",
        {
            "movie": movie,
            "video_id": video_id,
        },
    )


# ============================================================
# MOVIE LIST
# ============================================================

def movie_list(request):

    movies = (
        Movie.objects
        .all()
        .order_by("-id")
    )

    search_query = request.GET.get(
        "search"
    )

    genres = request.GET.getlist(
        "genres"
    )

    languages = request.GET.getlist(
        "languages"
    )

    if search_query:

        movies = movies.filter(
            name__icontains=search_query
        )

    if genres:

        movies = (
            movies
            .filter(
                genres__id__in=genres
            )
            .distinct()
        )

    if languages:

        movies = movies.filter(
            language__id__in=languages
        )

    paginator = Paginator(
        movies,
        6,
    )

    page_number = request.GET.get(
        "page"
    )

    page_obj = paginator.get_page(
        page_number
    )

    return render(
        request,
        "movies/movie_list.html",
        {
            "page_obj": page_obj,
            "genres": Genre.objects.all(),
            "languages": Language.objects.all(),
        },
    )


# ============================================================
# THEATER LIST
# ============================================================

def theater_list(request, movie_id):

    movie = get_object_or_404(
        Movie,
        id=movie_id,
    )

    theaters = Theater.objects.filter(
        movie=movie
    )

    return render(
        request,
        "movies/theater_list.html",
        {
            "movies": movie,
            "theaters": theaters,
        },
    )


# ============================================================
# BOOK SEATS
# TEMPORARY RESERVATION + RAZORPAY
# ============================================================

@login_required(login_url="/login/")
def book_seats(request, theater_id):

    theater = get_object_or_404(
        Theater,
        id=theater_id,
    )

    seats = (
        Seat.objects
        .filter(theater=theater)
        .order_by("seat_number")
    )

    now = timezone.now()

    available_seats = (
        seats
        .filter(is_booked=False)
        .exclude(
            reserved_by__isnull=False,
            reserved_until__gt=now,
        )
        .count()
    )

    # ========================================================
    # GET
    # ========================================================

    if request.method == "GET":

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
            },
        )

    # ========================================================
    # SELECTED SEATS
    # ========================================================

    selected_seats = request.POST.getlist(
        "seats"
    )

    if not selected_seats:

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": "Please select at least one seat.",
            },
        )

    # ========================================================
    # ONLY ONE SEAT PER PAYMENT
    # ========================================================

    if len(selected_seats) > 1:

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": (
                    "Please select only one seat per payment."
                ),
            },
        )

    seat_id = selected_seats[0]

    # ========================================================
    # IDEMPOTENCY KEY
    # ========================================================

    idempotency_key = (
        request.headers.get(
            "Idempotency-Key"
        )
        or request.POST.get(
            "idempotency_key"
        )
        or uuid.uuid4().hex
    )

    # ========================================================
    # CHECK EXISTING PAYMENT
    # ========================================================

    existing_payment = (
        Payment.objects
        .filter(
            idempotency_key=idempotency_key,
            booking__user=request.user,
        )
        .select_related(
            "booking",
            "booking__seat",
        )
        .first()
    )

    if existing_payment:

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if (
            existing_payment.payment_status
            == "SUCCESS"
        ):

            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "Payment already completed."
                    ),
                    "redirect_url": reverse(
                        "profile"
                    ),
                }
            )

        # ----------------------------------------------------
        # PENDING
        # ----------------------------------------------------

        if (
            existing_payment.payment_status
            == "PENDING"
        ):

            booking = existing_payment.booking
            seat = booking.seat

            now = timezone.now()

            if (
                booking.booking_status
                == "PENDING"
                and seat.reserved_by_id
                == request.user.id
                and seat.reserved_until
                and seat.reserved_until > now
            ):

                return render(
                    request,
                    "movies/payment.html",
                    {
                        "booking": booking,
                        "payment": existing_payment,
                        "razorpay_key_id": (
                            settings.RAZORPAY_KEY_ID
                        ),
                        "amount_paise": int(
                            existing_payment.amount * 100
                        ),
                    },
                )

            # ------------------------------------------------
            # OLD PENDING PAYMENT IS EXPIRED
            # ------------------------------------------------

            with transaction.atomic():

                old_payment = (
                    Payment.objects
                    .select_for_update()
                    .get(
                        id=existing_payment.id
                    )
                )

                old_booking = (
                    Booking.objects
                    .select_for_update()
                    .get(
                        id=old_payment.booking_id
                    )
                )

                old_seat = (
                    Seat.objects
                    .select_for_update()
                    .get(
                        id=old_booking.seat_id
                    )
                )

                if (
                    old_payment.payment_status
                    == "PENDING"
                ):

                    old_payment.payment_status = (
                        "TIMEOUT"
                    )

                    old_payment.save(
                        update_fields=[
                            "payment_status",
                            "updated_at",
                        ]
                    )

                if (
                    old_booking.booking_status
                    == "PENDING"
                ):

                    old_booking.booking_status = (
                        "EXPIRED"
                    )

                    old_booking.save(
                        update_fields=[
                            "booking_status",
                        ]
                    )

                old_seat.is_booked = False
                old_seat.reserved_by = None
                old_seat.reserved_until = None

                old_seat.save(
                    update_fields=[
                        "is_booked",
                        "reserved_by",
                        "reserved_until",
                    ]
                )

        # ----------------------------------------------------
        # FAILED / TIMEOUT / CANCELLED
        #
        # IMPORTANT:
        # Generate a fresh idempotency key.
        # ----------------------------------------------------

        idempotency_key = uuid.uuid4().hex

    # ========================================================
    # STEP 1
    # RESERVE SEAT + CREATE BOOKING
    # ========================================================

    try:

        with transaction.atomic():

            seat = (
                Seat.objects
                .select_for_update()
                .get(
                    id=seat_id,
                    theater=theater,
                )
            )

            now = timezone.now()

            # ------------------------------------------------
            # ALREADY BOOKED
            # ------------------------------------------------

            if seat.is_booked:

                raise SeatReservationError(
                    (
                        f"Seat {seat.seat_number} "
                        "is already booked."
                    )
                )

            # ------------------------------------------------
            # RELEASE EXPIRED RESERVATION
            # ------------------------------------------------

            if (
                seat.reserved_until
                and seat.reserved_until <= now
            ):

                seat.reserved_by = None
                seat.reserved_until = None

                seat.save(
                    update_fields=[
                        "reserved_by",
                        "reserved_until",
                    ]
                )

            # ------------------------------------------------
            # ACTIVE RESERVATION BY ANOTHER USER
            # ------------------------------------------------

            if (
                seat.reserved_by_id
                and seat.reserved_until
                and seat.reserved_until > now
                and seat.reserved_by_id
                != request.user.id
            ):

                raise SeatReservationError(
                    (
                        f"Seat {seat.seat_number} "
                        "is temporarily reserved."
                    )
                )

            # ------------------------------------------------
            # 2 MINUTE RESERVATION
            # ------------------------------------------------

            seat.reserved_by = request.user

            seat.reserved_until = (
                now
                + timedelta(
                    minutes=SEAT_RESERVATION_MINUTES
                )
            )

            seat.save(
                update_fields=[
                    "reserved_by",
                    "reserved_until",
                ]
            )

            # ------------------------------------------------
            # CREATE PENDING BOOKING
            # ------------------------------------------------

            booking = Booking.objects.create(
                user=request.user,
                seat=seat,
                movie=theater.movie,
                theater=theater,
                booking_status="PENDING",
            )

    except SeatReservationError as e:

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": str(e),
            },
        )

    except Exception as e:

        print(
            "BOOKING DATABASE ERROR:",
            str(e)
        )

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": (
                    "Unable to create booking."
                ),
            },
        )

    # ========================================================
    # STEP 2
    # CREATE RAZORPAY ORDER
    #
    # This is outside DB transaction.
    # If Razorpay fails, we safely cancel booking/release seat.
    # ========================================================

    try:

        amount_paise = int(
            PAYMENT_AMOUNT * 100
        )

        razorpay_order = client.order.create(
            data={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": (
                    f"BOOKING-{booking.id}"
                ),
                "notes": {
                    "booking_id": str(
                        booking.id
                    ),
                    "user_id": str(
                        request.user.id
                    ),
                    "seat_id": str(
                        booking.seat_id
                    ),
                },
            }
        )

    except Exception as e:

        print(
            "RAZORPAY ORDER ERROR:",
            str(e)
        )

        with transaction.atomic():

            booking = (
                Booking.objects
                .select_for_update()
                .get(
                    id=booking.id
                )
            )

            seat = (
                Seat.objects
                .select_for_update()
                .get(
                    id=booking.seat_id
                )
            )

            booking.booking_status = (
                "CANCELLED"
            )

            booking.save(
                update_fields=[
                    "booking_status"
                ]
            )

            seat.is_booked = False
            seat.reserved_by = None
            seat.reserved_until = None

            seat.save(
                update_fields=[
                    "is_booked",
                    "reserved_by",
                    "reserved_until",
                ]
            )

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": (
                    "Unable to create payment order."
                ),
            },
        )

    # ========================================================
    # STEP 3
    # CREATE PAYMENT
    # ========================================================

    try:

        payment = Payment.objects.create(
            booking=booking,
            razorpay_order_id=(
                razorpay_order["id"]
            ),
            amount=PAYMENT_AMOUNT,
            payment_status="PENDING",
            idempotency_key=idempotency_key,
        )

    except Exception as e:

        print(
            "PAYMENT DATABASE ERROR:",
            str(e)
        )

        with transaction.atomic():

            booking = (
                Booking.objects
                .select_for_update()
                .get(
                    id=booking.id
                )
            )

            seat = (
                Seat.objects
                .select_for_update()
                .get(
                    id=booking.seat_id
                )
            )

            booking.booking_status = (
                "CANCELLED"
            )

            booking.save(
                update_fields=[
                    "booking_status"
                ]
            )

            seat.is_booked = False
            seat.reserved_by = None
            seat.reserved_until = None

            seat.save(
                update_fields=[
                    "is_booked",
                    "reserved_by",
                    "reserved_until",
                ]
            )

        return render(
            request,
            "movies/seat_selection.html",
            {
                "theater": theater,
                "theaters": theater,
                "seats": seats,
                "available_seats": available_seats,
                "error": (
                    "Unable to create payment record."
                ),
            },
        )

    # ========================================================
    # PAYMENT PAGE
    # ========================================================

    return render(
        request,
        "movies/payment.html",
        {
            "booking": booking,
            "payment": payment,
            "razorpay_key_id": (
                settings.RAZORPAY_KEY_ID
            ),
            "amount_paise": int(
                payment.amount * 100
            ),
        },
    )


# ============================================================
# PAYMENT SUCCESS
# ============================================================

@login_required(login_url="/login/")
def payment_success(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "POST request required."
                ),
            },
            status=405,
        )

    try:

        data = json.loads(
            request.body.decode("utf-8")
        )

    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Invalid request data."
                ),
            },
            status=400,
        )

    order_id = data.get(
        "razorpay_order_id"
    )

    payment_id = data.get(
        "razorpay_payment_id"
    )

    signature = data.get(
        "razorpay_signature"
    )

    if not all(
        [
            order_id,
            payment_id,
            signature,
        ]
    ):

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Missing payment information."
                ),
            },
            status=400,
        )

    # ========================================================
    # FIND PAYMENT
    # ========================================================

    payment = (
        Payment.objects
        .filter(
            razorpay_order_id=order_id,
            booking__user=request.user,
        )
        .select_related(
            "booking",
            "booking__seat",
        )
        .first()
    )

    if not payment:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Payment record not found."
                ),
            },
            status=404,
        )

    # ========================================================
    # ALREADY SUCCESS
    # ========================================================

    if payment.payment_status == "SUCCESS":

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Payment already verified."
                ),
                "redirect_url": reverse(
                    "profile"
                ),
            }
        )

    # ========================================================
    # VERIFY RAZORPAY SIGNATURE
    # ========================================================

    try:

        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )

    except razorpay.errors.SignatureVerificationError:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Invalid payment signature."
                ),
            },
            status=400,
        )

    # ========================================================
    # ATOMIC CONFIRMATION
    # ========================================================

    with transaction.atomic():

        payment = (
            Payment.objects
            .select_for_update()
            .get(
                id=payment.id
            )
        )

        booking = (
            Booking.objects
            .select_for_update()
            .get(
                id=payment.booking_id
            )
        )

        seat = (
            Seat.objects
            .select_for_update()
            .get(
                id=booking.seat_id
            )
        )

        # ----------------------------------------------------
        # ALREADY SUCCESS
        # ----------------------------------------------------

        if payment.payment_status == "SUCCESS":

            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "Payment already completed."
                    ),
                    "redirect_url": reverse(
                        "profile"
                    ),
                }
            )

        # ----------------------------------------------------
        # PAYMENT FAILED / TIMEOUT
        # ----------------------------------------------------

        if payment.payment_status in [
            "FAILED",
            "TIMEOUT",
        ]:

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "This payment is no longer "
                        "active. Please create a "
                        "new booking."
                    ),
                },
                status=409,
            )

        # ----------------------------------------------------
        # BOOKING MUST BE PENDING
        # ----------------------------------------------------

        if booking.booking_status != "PENDING":

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "This booking is no longer "
                        "active. Please create a "
                        "new booking."
                    ),
                },
                status=409,
            )

        now = timezone.now()

        # ----------------------------------------------------
        # CHECK SEAT
        # ----------------------------------------------------

        if (
            seat.is_booked
            or seat.reserved_by_id
            != request.user.id
            or not seat.reserved_until
            or seat.reserved_until <= now
        ):

            booking.booking_status = (
                "EXPIRED"
            )

            booking.save(
                update_fields=[
                    "booking_status"
                ]
            )

            payment.payment_status = (
                "TIMEOUT"
            )

            payment.save(
                update_fields=[
                    "payment_status",
                    "updated_at",
                ]
            )

            seat.is_booked = False
            seat.reserved_by = None
            seat.reserved_until = None

            seat.save(
                update_fields=[
                    "is_booked",
                    "reserved_by",
                    "reserved_until",
                ]
            )

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Your 2-minute seat "
                        "reservation has expired. "
                        "Please book again."
                    ),
                },
                status=409,
            )

        # ----------------------------------------------------
        # SAVE PAYMENT
        # ----------------------------------------------------

        payment.razorpay_payment_id = (
            payment_id
        )

        payment.razorpay_signature = (
            signature
        )

        payment.payment_status = (
            "SUCCESS"
        )

        payment.save(
            update_fields=[
                "razorpay_payment_id",
                "razorpay_signature",
                "payment_status",
                "updated_at",
            ]
        )

        # ----------------------------------------------------
        # CONFIRM BOOKING
        # ----------------------------------------------------

        booking.booking_status = (
            "CONFIRMED"
        )

        booking.save(
            update_fields=[
                "booking_status"
            ]
        )

        # ----------------------------------------------------
        # BOOK SEAT
        # ----------------------------------------------------

        seat.is_booked = True
        seat.reserved_by = None
        seat.reserved_until = None

        seat.save(
            update_fields=[
                "is_booked",
                "reserved_by",
                "reserved_until",
            ]
        )

    # ========================================================
    # SEND EMAIL
    # ========================================================

    try:

        send_booking_email.delay(
            booking.id
        )

    except Exception as e:

        print(
            "EMAIL ERROR:",
            str(e)
        )

    # ========================================================
    # SUCCESS
    # ========================================================

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Payment verified successfully."
            ),
            "redirect_url": reverse(
                "profile"
            ),
        }
    )


# ============================================================
# PAYMENT FAILED / CANCELLED
# ============================================================

@login_required(login_url="/login/")
def payment_failed(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "POST request required."
                ),
            },
            status=405,
        )

    try:

        data = json.loads(
            request.body.decode("utf-8")
        )

    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):

        data = {}

    booking_id = data.get(
        "booking_id"
    )

    if not booking_id:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Booking ID is required."
                ),
            },
            status=400,
        )

    with transaction.atomic():

        booking = (
            Booking.objects
            .select_for_update()
            .filter(
                id=booking_id,
                user=request.user,
            )
            .first()
        )

        if not booking:

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Booking not found."
                    ),
                },
                status=404,
            )

        payment = (
            Payment.objects
            .select_for_update()
            .filter(
                booking=booking
            )
            .first()
        )

        seat = (
            Seat.objects
            .select_for_update()
            .get(
                id=booking.seat_id
            )
        )

        # ----------------------------------------------------
        # NEVER CANCEL SUCCESS
        # ----------------------------------------------------

        if (
            payment
            and payment.payment_status
            == "SUCCESS"
        ):

            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "Payment was already successful."
                    ),
                }
            )

        # ----------------------------------------------------
        # FAILED
        # ----------------------------------------------------

        if payment:

            payment.payment_status = (
                "FAILED"
            )

            payment.save(
                update_fields=[
                    "payment_status",
                    "updated_at",
                ]
            )

        # ----------------------------------------------------
        # CANCEL BOOKING
        # ----------------------------------------------------

        if (
            booking.booking_status
            == "PENDING"
        ):

            booking.booking_status = (
                "CANCELLED"
            )

            booking.save(
                update_fields=[
                    "booking_status"
                ]
            )

        # ----------------------------------------------------
        # RELEASE SEAT
        # ----------------------------------------------------

        if (
            seat.reserved_by_id
            == request.user.id
            and not seat.is_booked
        ):

            seat.reserved_by = None
            seat.reserved_until = None

            seat.save(
                update_fields=[
                    "reserved_by",
                    "reserved_until",
                ]
            )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Payment failed/cancelled. "
                "Seat has been released."
            ),
        }
    )


# ============================================================
# RAZORPAY WEBHOOK
# ============================================================

@csrf_exempt
def razorpay_webhook(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "POST request required."
                ),
            },
            status=405,
        )

    raw_body = request.body

    received_signature = (
        request.headers.get(
            "X-Razorpay-Signature"
        )
    )

    if not received_signature:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Missing webhook signature."
                ),
            },
            status=400,
        )

    webhook_secret = getattr(
        settings,
        "RAZORPAY_WEBHOOK_SECRET",
        None,
    )

    if not webhook_secret:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Webhook secret is not configured."
                ),
            },
            status=500,
        )

    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        received_signature,
    ):

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Invalid webhook signature."
                ),
            },
            status=400,
        )

    event_id = request.headers.get(
        "x-razorpay-event-id"
    )

    if not event_id:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Missing event ID."
                ),
            },
            status=400,
        )

    try:

        payload = json.loads(
            raw_body.decode("utf-8")
        )

    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Invalid webhook JSON."
                ),
            },
            status=400,
        )

    event_type = payload.get(
        "event"
    )

    # ========================================================
    # DUPLICATE WEBHOOK
    # ========================================================

    try:

        webhook_event, created = (
            RazorpayWebhookEvent.objects
            .get_or_create(
                event_id=event_id,
                defaults={
                    "event_type": (
                        event_type
                        or "unknown"
                    ),
                    "processed": False,
                },
            )
        )

    except IntegrityError:

        webhook_event = (
            RazorpayWebhookEvent.objects.get(
                event_id=event_id
            )
        )

        created = False

    if (
        not created
        and webhook_event.processed
    ):

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Duplicate webhook ignored."
                ),
            }
        )

    # ========================================================
    # PAYMENT CAPTURED
    # ========================================================

    if event_type == "payment.captured":

        try:

            payment_entity = (
                payload
                .get("payload", {})
                .get("payment", {})
                .get("entity", {})
            )

            razorpay_payment_id = (
                payment_entity.get("id")
            )

            razorpay_order_id = (
                payment_entity.get("order_id")
            )

            if not razorpay_payment_id:

                return JsonResponse(
                    {
                        "success": False,
                        "message": (
                            "Payment ID missing."
                        ),
                    },
                    status=400,
                )

            if not razorpay_order_id:

                return JsonResponse(
                    {
                        "success": False,
                        "message": (
                            "Order ID missing."
                        ),
                    },
                    status=400,
                )

            payment = (
                Payment.objects
                .filter(
                    razorpay_order_id=(
                        razorpay_order_id
                    )
                )
                .select_related(
                    "booking",
                    "booking__seat",
                )
                .first()
            )

            if not payment:

                webhook_event.processed = True
                webhook_event.processed_at = (
                    timezone.now()
                )

                webhook_event.save(
                    update_fields=[
                        "processed",
                        "processed_at",
                    ]
                )

                return JsonResponse(
                    {
                        "success": True,
                        "message": (
                            "Payment record not found. "
                            "Webhook acknowledged."
                        ),
                    }
                )

            with transaction.atomic():

                payment = (
                    Payment.objects
                    .select_for_update()
                    .get(
                        id=payment.id
                    )
                )

                booking = (
                    Booking.objects
                    .select_for_update()
                    .get(
                        id=payment.booking_id
                    )
                )

                seat = (
                    Seat.objects
                    .select_for_update()
                    .get(
                        id=booking.seat_id
                    )
                )

                # --------------------------------------------
                # ALREADY SUCCESS
                # --------------------------------------------

                if (
                    payment.payment_status
                    == "SUCCESS"
                ):

                    webhook_event.processed = True
                    webhook_event.processed_at = (
                        timezone.now()
                    )

                    webhook_event.save(
                        update_fields=[
                            "processed",
                            "processed_at",
                        ]
                    )

                    return JsonResponse(
                        {
                            "success": True,
                            "message": (
                                "Payment already processed."
                            ),
                        }
                    )

                # --------------------------------------------
                # PAYMENT TIMEOUT
                # --------------------------------------------

                if is_payment_expired(
                    payment
                ):

                    expire_payment(
                        payment
                    )

                    webhook_event.processed = True
                    webhook_event.processed_at = (
                        timezone.now()
                    )

                    webhook_event.save(
                        update_fields=[
                            "processed",
                            "processed_at",
                        ]
                    )

                    return JsonResponse(
                        {
                            "success": True,
                            "message": (
                                "Payment arrived after timeout."
                            ),
                        }
                    )

                # --------------------------------------------
                # BOOKING STATUS
                # --------------------------------------------

                if (
                    booking.booking_status
                    != "PENDING"
                ):

                    webhook_event.processed = True
                    webhook_event.processed_at = (
                        timezone.now()
                    )

                    webhook_event.save(
                        update_fields=[
                            "processed",
                            "processed_at",
                        ]
                    )

                    return JsonResponse(
                        {
                            "success": True,
                            "message": (
                                "Booking is not pending."
                            ),
                        }
                    )

                # --------------------------------------------
                # SEAT ALREADY BOOKED
                # --------------------------------------------

                if seat.is_booked:

                    return JsonResponse(
                        {
                            "success": False,
                            "message": (
                                "Seat is already booked."
                            ),
                        },
                        status=409,
                    )

                # --------------------------------------------
                # RESERVATION CHECK
                # --------------------------------------------

                now = timezone.now()

                if (
                    seat.reserved_by_id
                    != booking.user_id
                    or not seat.reserved_until
                    or seat.reserved_until <= now
                ):

                    expire_payment(
                        payment
                    )

                    webhook_event.processed = True
                    webhook_event.processed_at = (
                        timezone.now()
                    )

                    webhook_event.save(
                        update_fields=[
                            "processed",
                            "processed_at",
                        ]
                    )

                    return JsonResponse(
                        {
                            "success": True,
                            "message": (
                                "Seat reservation expired."
                            ),
                        }
                    )

                # --------------------------------------------
                # PAYMENT SUCCESS
                # --------------------------------------------

                payment.payment_status = (
                    "SUCCESS"
                )

                payment.razorpay_payment_id = (
                    razorpay_payment_id
                )

                normalized_method = (
                    normalize_payment_method(
                        payment_entity.get(
                            "method"
                        )
                    )
                )

                if normalized_method:

                    payment.payment_method = (
                        normalized_method
                    )

                payment.save(
                    update_fields=[
                        "payment_status",
                        "razorpay_payment_id",
                        "payment_method",
                        "updated_at",
                    ]
                )

                # --------------------------------------------
                # CONFIRM BOOKING
                # --------------------------------------------

                booking.booking_status = (
                    "CONFIRMED"
                )

                booking.save(
                    update_fields=[
                        "booking_status"
                    ]
                )

                # --------------------------------------------
                # BOOK SEAT
                # --------------------------------------------

                seat.is_booked = True
                seat.reserved_by = None
                seat.reserved_until = None

                seat.save(
                    update_fields=[
                        "is_booked",
                        "reserved_by",
                        "reserved_until",
                    ]
                )

                # --------------------------------------------
                # WEBHOOK PROCESSED
                # --------------------------------------------

                webhook_event.processed = True
                webhook_event.processed_at = (
                    timezone.now()
                )

                webhook_event.save(
                    update_fields=[
                        "processed",
                        "processed_at",
                    ]
                )

            # ------------------------------------------------
            # EMAIL
            # ------------------------------------------------

            try:

                send_booking_email.delay(
                    booking.id
                )

            except Exception as e:

                print(
                    "Webhook email error:",
                    str(e)
                )

            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "payment.captured processed."
                    ),
                }
            )

        except Exception as e:

            print(
                "Webhook payment.captured error:",
                str(e)
            )

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Webhook processing failed."
                    ),
                },
                status=500,
            )

    # ========================================================
    # PAYMENT FAILED
    # ========================================================

    elif event_type == "payment.failed":

        try:

            payment_entity = (
                payload
                .get("payload", {})
                .get("payment", {})
                .get("entity", {})
            )

            razorpay_payment_id = (
                payment_entity.get("id")
            )

            razorpay_order_id = (
                payment_entity.get("order_id")
            )

            if not razorpay_order_id:

                webhook_event.processed = True
                webhook_event.processed_at = (
                    timezone.now()
                )

                webhook_event.save(
                    update_fields=[
                        "processed",
                        "processed_at",
                    ]
                )

                return JsonResponse(
                    {
                        "success": True,
                        "message": (
                            "Order ID missing; "
                            "webhook acknowledged."
                        ),
                    }
                )

            payment = (
                Payment.objects
                .filter(
                    razorpay_order_id=(
                        razorpay_order_id
                    )
                )
                .select_related(
                    "booking",
                    "booking__seat",
                )
                .first()
            )

            if payment:

                with transaction.atomic():

                    payment = (
                        Payment.objects
                        .select_for_update()
                        .get(
                            id=payment.id
                        )
                    )

                    booking = (
                        Booking.objects
                        .select_for_update()
                        .get(
                            id=payment.booking_id
                        )
                    )

                    seat = (
                        Seat.objects
                        .select_for_update()
                        .get(
                            id=booking.seat_id
                        )
                    )

                    # ----------------------------------------
                    # DO NOT CHANGE SUCCESS
                    # ----------------------------------------

                    if (
                        payment.payment_status
                        == "SUCCESS"
                    ):

                        webhook_event.processed = True
                        webhook_event.processed_at = (
                            timezone.now()
                        )

                        webhook_event.save(
                            update_fields=[
                                "processed",
                                "processed_at",
                            ]
                        )

                        return JsonResponse(
                            {
                                "success": True,
                                "message": (
                                    "Payment already successful."
                                ),
                            }
                        )

                    # ----------------------------------------
                    # MARK FAILED
                    # ----------------------------------------

                    payment.payment_status = (
                        "FAILED"
                    )

                    if razorpay_payment_id:

                        payment.razorpay_payment_id = (
                            razorpay_payment_id
                        )

                    payment.save(
                        update_fields=[
                            "payment_status",
                            "razorpay_payment_id",
                            "updated_at",
                        ]
                    )

                    # ----------------------------------------
                    # CANCEL PENDING BOOKING
                    # ----------------------------------------

                    if (
                        booking.booking_status
                        == "PENDING"
                    ):

                        booking.booking_status = (
                            "CANCELLED"
                        )

                        booking.save(
                            update_fields=[
                                "booking_status"
                            ]
                        )

                    # ----------------------------------------
                    # RELEASE SEAT
                    # ----------------------------------------

                    if not seat.is_booked:

                        seat.reserved_by = None
                        seat.reserved_until = None

                        seat.save(
                            update_fields=[
                                "reserved_by",
                                "reserved_until",
                            ]
                        )

            webhook_event.processed = True
            webhook_event.processed_at = (
                timezone.now()
            )

            webhook_event.save(
                update_fields=[
                    "processed",
                    "processed_at",
                ]
            )

            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "payment.failed processed."
                    ),
                }
            )

        except Exception as e:

            print(
                "Webhook payment.failed error:",
                str(e)
            )

            return JsonResponse(
                {
                    "success": False,
                    "message": (
                        "Failed webhook processing."
                    ),
                },
                status=500,
            )

    # ========================================================
    # OTHER EVENTS
    # ========================================================

    webhook_event.processed = True
    webhook_event.processed_at = (
        timezone.now()
    )

    webhook_event.save(
        update_fields=[
            "processed",
            "processed_at",
        ]
    )

    return JsonResponse(
        {
            "success": True,
            "message": (
                f"Event {event_type} acknowledged."
            ),
        }
    )


# ============================================================
# TEMPORARY SEAT RESERVATION API
# ============================================================

@login_required(login_url="/login/")
def reserve_seat_view(request, seat_id):

    if request.method != "POST":

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "POST request required."
                ),
            },
            status=405,
        )

    try:

        seat = reserve_seat(
            seat_id=seat_id,
            user=request.user,
        )

        return JsonResponse(
            {
                "success": True,
                "message": (
                    "Seat reserved successfully "
                    "for 2 minutes."
                ),
                "seat_id": seat.id,
                "seat_number": seat.seat_number,
                "reserved_until": (
                    seat.reserved_until.isoformat()
                ),
            }
        )

    except SeatReservationError as e:

        return JsonResponse(
            {
                "success": False,
                "message": str(e),
            },
            status=409,
        )

    except Seat.DoesNotExist:

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Seat not found."
                ),
            },
            status=404,
        )

    except Exception as e:

        print(
            "Seat reservation error:",
            str(e)
        )

        return JsonResponse(
            {
                "success": False,
                "message": (
                    "Unable to reserve seat."
                ),
            },
            status=500,
        )


# ============================================================
# ADMIN ACCESS
# ============================================================

def admin_required(user):

    return (
        user.is_authenticated
        and user.is_active
        and user.is_staff
    )


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@login_required(login_url="/login/")
@user_passes_test(
    admin_required,
    login_url="/login/",
)
def admin_dashboard(request):

    cache_key = (
        "bookmyseat_admin_analytics"
    )

    analytics = cache.get(
        cache_key
    )

    if analytics is None:

        analytics = (
            get_admin_analytics()
        )

        cache.set(
            cache_key,
            analytics,
            timeout=60,
        )

    return render(
        request,
        "admin_dashboard.html",
        {
            "analytics": analytics,
        },
    )


# ============================================================
# ADMIN ANALYTICS API
# ============================================================

@login_required(login_url="/login/")
@user_passes_test(
    admin_required,
    login_url="/login/",
)
def admin_analytics_api(request):

    if request.method != "GET":

        return JsonResponse(
            {
                "error": (
                    "Only GET requests are allowed."
                )
            },
            status=405,
        )

    cache_key = (
        "bookmyseat_admin_analytics"
    )

    analytics = cache.get(
        cache_key
    )

    if analytics is None:

        analytics = (
            get_admin_analytics()
        )

        cache.set(
            cache_key,
            analytics,
            timeout=60,
        )

    return JsonResponse(
        analytics
    )