"""Database tables.

The key rule lives in the database, not in application code: a slot can have at
most one *active* appointment (booked or held). A partial unique index enforces
it, so two people booking the same slot at the same moment can't both succeed,
however many app servers are running.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ACTIVE_STATUSES = ("booked", "held")
# What a calendar shows in a slot: live bookings plus visits that already happened.
SHOWN_STATUSES = (*ACTIVE_STATUSES, "completed", "no_show")


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    specialty: Mapped[str] = mapped_column(String(50))


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(30))
    # Demo personas appear in the "viewing as" picker; everyone else just fills the schedule.
    featured: Mapped[bool] = mapped_column(Boolean, default=False)


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (UniqueConstraint("provider_id", "start", name="uq_slot_provider_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime] = mapped_column(DateTime)
    # None = bookable; "hours" = outside the doctor's working hours; "time_off" = the doctor blocked it.
    blocked_reason: Mapped[str | None] = mapped_column(String(10), nullable=True)

    provider: Mapped[Provider] = relationship()


class WaitlistEntry(Base):
    """A patient who wants an earlier appointment than the ones currently open."""

    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    specialty: Mapped[str | None] = mapped_column(String(50), nullable=True)
    earliest: Mapped[date] = mapped_column(Date)
    latest: Mapped[date] = mapped_column(Date)
    time_of_day: Mapped[str] = mapped_column(String(10), default="any")  # any | morning | afternoon
    # waiting -> offered -> booked, or back to waiting if the offer is declined or expires
    status: Mapped[str] = mapped_column(String(10), default="waiting", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the patient already has a later appointment, accepting an offer releases it.
    replaces_appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1 = the doctor cancelled this patient's visit, so they go to the front of the line.
    priority: Mapped[int] = mapped_column(Integer, default=0)

    patient: Mapped[Patient] = relationship()
    provider: Mapped[Provider | None] = relationship()


class Appointment(Base):
    """booked | held (offered to a waitlisted patient) | cancelled | declined | expired
    | completed | no_show (set by the doctor after the visit time)"""

    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_one_active_appointment_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status IN ('booked', 'held')"),
            sqlite_where=text("status IN ('booked', 'held')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), index=True)
    source: Mapped[str] = mapped_column(String(10), default="direct")  # direct | waitlist | assistant
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    waitlist_entry_id: Mapped[int | None] = mapped_column(ForeignKey("waitlist.id"), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(String(2000), nullable=True)  # the doctor's visit note

    slot: Mapped[Slot] = relationship()
    patient: Mapped[Patient] = relationship()


class OutboxEvent(Base):
    """Events are written in the same transaction as the change that caused them
    (the transactional outbox pattern), then published to the queue. A crash between
    the two can't lose an event: unpublished rows are simply sent on the next pass."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Notification(Base):
    """Messages to patients. Delivery is simulated: a consumer marks them sent."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    patient: Mapped[Patient] = relationship()


class WorkingHours(Base):
    """A doctor's regular hours for one weekday (0 = Monday). No row for a day = not working.
    A doctor with no rows at all keeps every generated slot open."""

    __tablename__ = "working_hours"
    __table_args__ = (UniqueConstraint("provider_id", "weekday", name="uq_hours_provider_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes after midnight, e.g. 540 = 9:00
    end_minute: Mapped[int] = mapped_column(Integer)


class TimeOff(Base):
    """A block of time the doctor isn't available (a meeting, a day off, a conference)."""

    __tablename__ = "time_off"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class User(Base):
    """A login. Each user is either a patient or a doctor, linked to that record."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(10))  # patient | doctor
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class UserSession(Base):
    """A signed-in browser. Only a hash of the token is stored, so a database leak
    doesn't hand out live sessions; deleting the row signs that browser out."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
