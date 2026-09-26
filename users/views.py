from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from .forms import UserRegisterForm, UserUpdateForm
from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate
from django.contrib.auth.decorators import login_required
from movies.models import Movie, Booking


# =========================================
# Messages Page
# =========================================

def messages(request):
    return render(request, 'users/messages.html')


# =========================================
# Home Page
# =========================================

def home(request):
    movies = Movie.objects.all()

    return render(
        request,
        'home.html',
        {'movies': movies}
    )


# =========================================
# Register
# =========================================

def register(request):

    if request.method == 'POST':

        form = UserRegisterForm(request.POST)

        if form.is_valid():

            # Create user
            user = form.save()

            # Get username and password
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password1')

            # Authenticate user
            user = authenticate(
                username=username,
                password=password
            )

            # Login automatically after registration
            if user is not None:
                login(request, user)
                return redirect('/users/')

    else:
        form = UserRegisterForm()

    return render(
        request,
        'register.html',
        {'form': form}
    )


# =========================================
# Login
# =========================================

def login_view(request):

    if request.method == 'POST':

        form = AuthenticationForm(
            request,
            data=request.POST
        )

        if form.is_valid():

            user = form.get_user()

            # Login user
            login(request, user)

            # After login go to Home
            return redirect('/users/')

    else:

        form = AuthenticationForm()

    return render(
        request,
        'login.html',
        {'form': form}
    )


# =========================================
# Profile
# =========================================

@login_required
def profile(request):

    # Get user's bookings
    bookings = Booking.objects.filter(
        user=request.user
    )

    if request.method == 'POST':

        u_form = UserUpdateForm(
            request.POST,
            instance=request.user
        )

        if u_form.is_valid():

            u_form.save()

            return redirect('profile')

    else:

        u_form = UserUpdateForm(
            instance=request.user
        )

    return render(
        request,
        'profile.html',
        {
            'u_form': u_form,
            'bookings': bookings
        }
    )


# =========================================
# Change Password
# =========================================

@login_required
def reset_password(request):

    if request.method == 'POST':

        form = PasswordChangeForm(
            user=request.user,
            data=request.POST
        )

        if form.is_valid():

            form.save()

            # Keep user logged in after password change
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(
                request,
                form.user
            )

            return redirect('profile')

    else:

        form = PasswordChangeForm(
            user=request.user
        )

    return render(
        request,
        'reset_password.html',
        {'form': form}
    )