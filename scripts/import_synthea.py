"""Turn Synthea's FHIR output into the compact chart file the demo clinic loads.

Synthea (https://github.com/synthetichealth/synthea, Apache 2.0) generates realistic,
entirely synthetic patient records. Each patient's raw FHIR bundle is over 1 MB, so
this keeps only what a clinic chart needs: allergies, conditions, medications and
vital signs, with their standard codes (SNOMED CT, RxNorm, LOINC).

Dates are stored as days *relative to the reference date* (the day Synthea simulated
up to), and the seed shifts them to "today", so the demo's history never goes stale.

Regenerate the data (Java 17+):
  curl -L -o synthea.jar \\
    https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
  java -jar synthea.jar -p 80 -s 42 -cs 42 -r 20261018 \\
      --exporter.fhir.export=true --exporter.csv.export=false \\
      --exporter.hospital.fhir.export=false --exporter.practitioner.fhir.export=false \\
      --exporter.years_of_history=5 --generate.only_alive_patients=true Massachusetts
  python scripts/import_synthea.py output/fhir carequeue/data/synthea_charts.json --reference-date 2026-10-18
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

# Synthea records social circumstances as "findings" (employment, education, stress...).
# A clinic chart keeps disorders, plus the few findings a doctor actually tracks.
CLINICAL_FINDINGS = {"Body mass index 30+ - obesity (finding)", "Prediabetes (finding)", "Chronic pain (finding)",
                     "Anemia (disorder)", "Hyperlipidemia (disorder)"}
# LOINC codes for the vital signs a clinic chart shows.
VITALS = {"8480-6": "systolic", "8462-4": "diastolic", "8867-4": "heart_rate", "29463-7": "weight_kg",
          "39156-5": "bmi", "8310-5": "temperature_c", "8302-2": "height_cm"}
MAX_VITALS = 8  # the most recent readings
MAX_STOPPED_MEDS = 6  # recent past medications, for context


def clean(display: str) -> str:
    """'Peanut (substance)' -> 'Peanut'; 'Viral sinusitis (disorder)' -> 'Viral sinusitis'."""
    return re.sub(r"\s*\((substance|organism|disorder|finding|situation|product|morphologic abnormality)\)$",
                  "", display).strip()


def day(value: str | None, ref: date) -> int | None:
    """Days from the reference date: -30 means 30 days before it."""
    if not value:
        return None
    return (datetime.fromisoformat(value.replace("Z", "+00:00")).date() - ref).days


def first_coding(concept: dict) -> dict:
    return (concept.get("coding") or [{}])[0]


def convert(bundle: dict, ref: date) -> dict:
    by_type: dict[str, list] = {}
    for entry in bundle["entry"]:
        by_type.setdefault(entry["resource"]["resourceType"], []).append(entry["resource"])
    patient = by_type["Patient"][0]
    chart = {"source": f"synthea:{patient['id'][:8]}", "sex": patient.get("gender"),
             "birth_day": day(patient["birthDate"], ref), "allergies": [], "conditions": [], "medications": [],
             "vitals": []}

    for a in by_type.get("AllergyIntolerance", []):
        coding = first_coding(a["code"])
        if "disposition" in coding.get("display", ""):  # "Allergic disposition" is a tendency, not an allergy
            continue
        reaction = None
        if a.get("reaction"):
            reaction = clean(first_coding(a["reaction"][0]["manifestation"][0]).get("display", "")) or None
        status = first_coding(a.get("clinicalStatus", {})).get("code", "active")
        chart["allergies"].append({"substance": clean(coding.get("display", "")), "code": coding.get("code"),
                                   "system": coding.get("system"), "category": (a.get("category") or [None])[0],
                                   "criticality": a.get("criticality"), "reaction": reaction,
                                   "status": "active" if status == "active" else "inactive",
                                   "recorded_day": day(a.get("recordedDate"), ref)})

    seen: set[str] = set()
    for c in sorted(by_type.get("Condition", []), key=lambda r: r.get("onsetDateTime", "")):
        coding = first_coding(c["code"])
        display = coding.get("display", "")
        if not (display.endswith("(disorder)") or display in CLINICAL_FINDINGS) or coding.get("code") in seen:
            continue
        seen.add(coding.get("code"))
        status = first_coding(c.get("clinicalStatus", {})).get("code", "active")
        chart["conditions"].append({"name": clean(display), "code": coding.get("code"),
                                    "onset_day": day(c.get("onsetDateTime"), ref),
                                    "resolved_day": day(c.get("abatementDateTime"), ref),
                                    "status": "active" if status == "active" else "resolved"})

    meds: dict[str, dict] = {}
    for m in sorted(by_type.get("MedicationRequest", []), key=lambda r: r.get("authoredOn", "")):
        if "medicationCodeableConcept" not in m:
            continue
        coding = first_coding(m["medicationCodeableConcept"])
        name = coding.get("display", "")
        meds[name] = {"name": name, "code": coding.get("code"),
                      "status": "active" if m.get("status") == "active" else "stopped",
                      "start_day": meds.get(name, {}).get("start_day", day(m.get("authoredOn"), ref)),
                      "last_day": day(m.get("authoredOn"), ref)}
    active = [m for m in meds.values() if m["status"] == "active"]
    stopped = sorted((m for m in meds.values() if m["status"] == "stopped"), key=lambda m: m["last_day"] or 0)
    chart["medications"] = active + stopped[-MAX_STOPPED_MEDS:]

    readings: dict[str, dict] = {}
    for o in by_type.get("Observation", []):
        when = o.get("effectiveDateTime")
        if not when:
            continue
        parts = [(o["code"], o.get("valueQuantity"))] + [(c["code"], c.get("valueQuantity"))
                                                           for c in o.get("component", [])]
        for concept, quantity in parts:
            name = VITALS.get(first_coding(concept).get("code"))
            if name and quantity and "value" in quantity:
                readings.setdefault(when[:10], {"day": day(when, ref)})[name] = round(float(quantity["value"]), 1)
    useful = [r for r in readings.values() if "systolic" in r or "heart_rate" in r or "weight_kg" in r]
    chart["vitals"] = sorted(useful, key=lambda r: r["day"])[-MAX_VITALS:]
    return chart


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fhir_dir", help="Synthea's output/fhir folder")
    ap.add_argument("out")
    ap.add_argument("--reference-date", required=True, help="the date passed to Synthea with -r, e.g. 2026-10-18")
    args = ap.parse_args()
    ref = date.fromisoformat(args.reference_date)
    files = sorted(f for f in Path(args.fhir_dir).glob("*.json")
                   if not f.name.startswith(("hospitalInformation", "practitionerInformation")))
    charts = [convert(json.loads(f.read_text()), ref) for f in files]
    out = {"generator": "Synthea (synthetic patients, Apache 2.0) via scripts/import_synthea.py",
           "reference_date": ref.isoformat(), "days_are": "relative to reference_date",
           "count": len(charts), "charts": charts}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")) + "\n")
    kids = sum(1 for c in charts if c["birth_day"] > -18 * 365)
    print(f"{len(charts)} charts ({kids} children), {Path(args.out).stat().st_size / 1024:.0f} KB -> {args.out}")


if __name__ == "__main__":
    main()
