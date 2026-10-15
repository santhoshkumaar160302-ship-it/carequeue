from datetime import datetime

from sqlalchemy import select

from carequeue.models import Slot


def slot_at(clinic, provider_id: int, when: datetime) -> int:
    with clinic.Session() as s:
        return s.scalar(select(Slot.id).where(Slot.provider_id == provider_id, Slot.start == when))


def appointment(clinic, appt_id):
    from carequeue.models import Appointment

    with clinic.Session() as s:
        return s.get(Appointment, appt_id)


def held_for(clinic, patient_id):
    return [a for a in clinic.appointments(patient_id) if a["status"] == "held"]
