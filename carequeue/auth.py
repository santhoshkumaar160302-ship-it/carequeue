"""Sign-in: password hashing, sessions and roles.

- Passwords are hashed with scrypt (Python's standard library), with a random salt per user.
- A session is a random token kept in an HttpOnly cookie. The database stores only its
  SHA-256 hash, so a leaked database doesn't hand out live sessions.
- Each user is a patient or a doctor. The API checks the role on every request and always
  uses the signed-in user's own patient or doctor id, never one sent by the browser.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .clinic import ClinicError
from .clock import Clock
from .models import Patient, Provider, User, UserSession

SESSION_DAYS = 7
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEMO_PATIENT = "harper.lopez@demo.carequeue"
DEMO_DOCTOR = "maya.patel@demo.carequeue"


class AuthError(ClinicError):
    status = 401


class Forbidden(ClinicError):
    status = 403


class TooManyAttempts(ClinicError):
    status = 429


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(candidate.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, session_factory, clock: Clock, max_attempts: int = 5, window_s: int = 300):
        self.Session = session_factory
        self.clock = clock
        self.max_attempts, self.window = max_attempts, timedelta(seconds=window_s)
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        # Spent on unknown emails too, so response time doesn't reveal which emails exist.
        self._dummy_hash = hash_password(secrets.token_hex(8))

    # ------------------------------------------------------------------ throttle
    def _check_throttle(self, key: str) -> None:
        now = self.clock.now()
        with self._lock:
            q = self._failures[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_attempts:
                raise TooManyAttempts("Too many sign-in attempts. Try again in a few minutes.")

    def _record_failure(self, key: str) -> None:
        with self._lock:
            self._failures[key].append(self.clock.now())

    # ------------------------------------------------------------------ sessions
    def _start_session(self, s, user: User) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock.now()
        s.add(UserSession(token_hash=_token_hash(token), user_id=user.id, created_at=now,
                          expires_at=now + timedelta(days=SESSION_DAYS)))
        s.execute(delete(UserSession).where(UserSession.expires_at <= now))
        s.commit()
        return token

    def login(self, email: str, password: str, client: str = "") -> str:
        email = email.strip().lower()
        key = f"{email}|{client}"
        self._check_throttle(key)
        with self.Session() as s:
            user = s.scalar(select(User).where(User.email == email))
            ok = verify_password(password, user.password_hash if user else self._dummy_hash) and user is not None
            if not ok:
                self._record_failure(key)
                raise AuthError("That email and password don't match.")
            return self._start_session(s, user)

    def demo_login(self, role: str) -> str:
        email = {"patient": DEMO_PATIENT, "doctor": DEMO_DOCTOR}.get(role)
        if email is None:
            raise ClinicError("role must be patient or doctor")
        with self.Session() as s:
            user = s.scalar(select(User).where(User.email == email))
            if user is None:
                raise ClinicError("The demo accounts aren't set up on this server.")
            return self._start_session(s, user)

    def register(self, name: str, email: str, phone: str, password: str) -> str:
        """Create a patient account. Doctor accounts are created by the clinic, not self-service."""
        name, email, phone = name.strip(), email.strip().lower(), phone.strip()
        if not 2 <= len(name) <= 100:
            raise ClinicError("Please enter your full name.")
        if not EMAIL_RE.match(email) or len(email) > 200:
            raise ClinicError("Please enter a valid email address.")
        if len(password) < 8:
            raise ClinicError("Use a password of at least 8 characters.")
        if len(password) > 200:
            raise ClinicError("That password is too long.")
        now = self.clock.now()
        with self.Session() as s:
            if s.scalar(select(func.count(User.id)).where(User.email == email)):
                raise ClinicError("An account with that email already exists. Try signing in.")
            patient = Patient(name=name, phone=phone[:30] or "-", featured=False)
            s.add(patient)
            s.flush()
            user = User(email=email, password_hash=hash_password(password), role="patient",
                        patient_id=patient.id, created_at=now)
            s.add(user)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                raise ClinicError("An account with that email already exists. Try signing in.") from None
            return self._start_session(s, user)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.Session() as s:
            s.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
            s.commit()

    def user_for(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = self.clock.now()
        with self.Session() as s:
            row = s.execute(select(UserSession, User).join(User, UserSession.user_id == User.id)
                            .where(UserSession.token_hash == _token_hash(token))).first()
            if row is None or row[0].expires_at <= now:
                return None
            user = row[1]
            if user.role == "patient":
                who = s.get(Patient, user.patient_id)
                profile = {"patient_id": who.id, "name": who.name}
            else:
                who = s.get(Provider, user.provider_id)
                profile = {"provider_id": who.id, "name": who.name, "specialty": who.specialty}
            return {"id": user.id, "email": user.email, "role": user.role, **profile}


def seed_users(session, now: datetime, password: str | None = None) -> None:
    """Accounts for the demo: every doctor, and every featured patient.

    Their password is random, so the only way in to a fresh demo is the one-click demo
    buttons (switched off with CQ_DEMO_LOGINS=0). New patients can register themselves.
    """
    if session.scalar(select(func.count(User.id))):
        return
    secret = hash_password(password or secrets.token_urlsafe(18))

    def email(name: str) -> str:
        first, last = name.replace("Dr. ", "").split(" ", 1)
        return f"{first}.{last.replace(' ', '')}@demo.carequeue".lower()

    for p in session.scalars(select(Provider)):
        session.add(User(email=email(p.name), password_hash=secret, role="doctor", provider_id=p.id, created_at=now))
    for p in session.scalars(select(Patient).where(Patient.featured.is_(True))):
        session.add(User(email=email(p.name), password_hash=secret, role="patient", patient_id=p.id, created_at=now))
    session.commit()
