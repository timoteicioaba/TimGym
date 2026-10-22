"""Normalize Apple Health exports for storage.

Two payload shapes are accepted:

* Health Auto Export's REST API automations (JSON, version 2 recommended), for
  workouts, daily activity totals, and body measurements (weight, body fat, waist):
  {"data": {"workouts": [...], "metrics": [...]}}
* A simple shape for Shortcuts or scripts:
  {"workouts": [{"id", "name", "start", "duration_min", "active_kcal", "distance_km"}],
   "daily": [{"date", "active_kcal", "steps"}]}

Entries that cannot be read are skipped and counted, not guessed.
"""
import re
from datetime import date, datetime

_KJ_PER_KCAL = 4.184
_KM_PER_UNIT = {"km": 1.0, "m": 0.001, "mi": 1.609344, "yd": 0.0009144, "ft": 0.0003048}
# Health Auto Export metric name -> stored metric name
_DAILY_METRICS = {"active_energy": "active_kcal", "step_count": "steps"}
# Health Auto Export metric name -> (body measurement field, plausible range after conversion)
_BODY_METRICS = {
    "weight_body_mass": ("weight_kg", 20, 400),
    "body_fat_percentage": ("body_fat_pct", 2, 75),
    "waist_circumference": ("waist_cm", 30, 250),
}
_CM_PER_UNIT = {"cm": 1.0, "in": 2.54, "m": 100.0, "ft": 30.48, "mm": 0.1}
MAX_ENTRIES = 20000
MAX_SAMPLES_PER_METRIC = 200000  # a year of minute-level samples is still under this


def _quantity(value):
    """Return (amount, units) from 12.5, {"qty": 12.5, "units": "kcal"}, or a list of those."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        try:
            return float(value), None
        except ValueError:
            return None, None
    if isinstance(value, dict):
        amount, _ = _quantity(value.get("qty"))
        return amount, str(value.get("units") or "").strip() or None
    if isinstance(value, list):
        total, units = None, None
        for item in value:
            amount, item_units = _quantity(item)
            if amount is not None:
                total = (total or 0.0) + amount
                units = units or item_units
        return total, units
    return None, None


def _kcal(value):
    amount, units = _quantity(value)
    if amount is None:
        return None
    if units and units.lower() == "kj":
        amount /= _KJ_PER_KCAL
    return round(amount, 1) if 0 <= amount <= 20000 else None


def _km(value):
    amount, units = _quantity(value)
    if amount is None:
        return None
    amount *= _KM_PER_UNIT.get((units or "km").lower(), 1.0)
    return round(amount, 3) if 0 <= amount <= 1000 else None


_LOOSE_TIMESTAMP_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.\d+)?)?"
    r"\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?\s*(?P<offset>Z|[+-]\d{2}:?\d{2})?"
)


def _timestamp(value):
    """Parse "2026-10-21 18:05:12 +0300" (Health Auto Export), ISO 8601, or the
    12-hour form an iPhone set to AM/PM produces ("2026-10-21 6:05:12 PM +0300")."""
    # iOS puts a narrow no-break space before AM/PM.
    text = re.sub(r"[\u00a0\u202f]", " ", str(value or "")).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    match = _LOOSE_TIMESTAMP_RE.match(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    ampm = (match.group("ampm") or "").replace(".", "").upper()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "PM" else 0)
    offset = match.group("offset") or ""
    if offset and offset != "Z" and ":" not in offset:
        offset = offset[:3] + ":" + offset[3:]
    try:
        return datetime.fromisoformat(
            f'{match.group("date")}T{hour:02d}:{match.group("minute")}:{match.group("second") or "00"}'
            + ("+00:00" if offset == "Z" else offset)
        )
    except ValueError:
        return None


def _workout(item):
    """Return (workout, None) or (None, reason it was skipped)."""
    if not isinstance(item, dict):
        return None, "a workout entry was not an object"
    name = str(item.get("name") or item.get("type") or "").strip()[:80]
    started = _timestamp(item.get("start"))
    if not name:
        return None, "a workout had no name"
    if started is None:
        return None, f'{name}: unreadable start time {str(item.get("start"))[:40]!r}'

    duration = None
    if item.get("duration_min") is not None:
        amount, _ = _quantity(item.get("duration_min"))
        duration = amount * 60 if amount is not None else None
    elif item.get("duration") is not None:
        duration, _ = _quantity(item.get("duration"))  # seconds
    if duration is None:
        ended = _timestamp(item.get("end"))
        duration = (ended - started).total_seconds() if ended else None
    if duration is not None and not 0 <= duration <= 48 * 3600:
        duration = None

    energy = None
    for key in ("active_kcal", "activeEnergyBurned", "activeEnergy", "totalEnergy"):
        if item.get(key) is not None:
            energy = _kcal(item[key])
            if energy is not None:
                break
    distance = _km(item["distance_km"]) if item.get("distance_km") is not None else _km(item.get("distance"))

    return {
        # Without an id, the start time and activity identify a workout across re-sends.
        "external_id": str(item.get("id") or f"{started.isoformat()}|{name}")[:200],
        "workout_date": started.date().isoformat(),
        "started_at": started.isoformat(),
        "activity": name,
        "duration_s": int(round(duration)) if duration is not None else None,
        "active_kcal": energy,
        "distance_km": distance,
    }, None


def _body_value(field, qty, units):
    amount, _ = _quantity(qty)
    if amount is None:
        return None
    units = str(units or "").strip().lower()
    if field == "weight_kg":
        if units in ("lb", "lbs", "pound", "pounds"):
            amount *= 0.45359237
        elif units == "g":
            amount /= 1000
    elif field == "body_fat_pct" and amount <= 1:
        amount *= 100  # Apple Health stores body fat as a fraction
    elif field == "waist_cm":
        amount *= _CM_PER_UNIT.get(units, 1.0)
    return amount


def _day(value):
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_health_payload(payload):
    """Return {"workouts": [...], "daily": {(day, metric): value}, "body": {day: {field: value}}, "skipped": n,
    "skipped_reasons": [...], "received": {"workouts": n, "metrics": [names]}}."""
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    # Exporters send null for sections that were not selected.
    raw_workouts = data.get("workouts", []) if data.get("workouts") is not None else []
    raw_metrics = data.get("metrics", []) if data.get("metrics") is not None else []
    raw_daily = data.get("daily", []) if data.get("daily") is not None else []
    if not all(isinstance(part, list) for part in (raw_workouts, raw_metrics, raw_daily)):
        raise ValueError("workouts, metrics, and daily must be lists.")
    if len(raw_workouts) > MAX_ENTRIES or len(raw_daily) > MAX_ENTRIES:
        raise ValueError(f"Send at most {MAX_ENTRIES} entries at a time.")

    skipped = 0
    reasons = []
    workouts = {}
    for item in raw_workouts:
        workout, reason = _workout(item)
        if workout is None:
            skipped += 1
            if reason not in reasons and len(reasons) < 5:
                reasons.append(reason)
        else:
            workouts[workout["external_id"]] = workout

    daily = {}
    body_samples = {}  # (day, field) -> [values]
    for metric in raw_metrics:
        if isinstance(metric, dict) and metric.get("name") in _BODY_METRICS:
            field, low, high = _BODY_METRICS[metric["name"]]
            samples = metric.get("data") or []
            if not isinstance(samples, list) or len(samples) > MAX_SAMPLES_PER_METRIC:
                skipped += 1
                continue
            for sample in samples:
                day = _day(sample.get("date")) if isinstance(sample, dict) else None
                value = _body_value(field, sample.get("qty") if isinstance(sample, dict) else None, metric.get("units"))
                if day is None or value is None or not low <= value <= high:
                    skipped += 1
                    if len(reasons) < 5:
                        reason = f'{metric["name"]}: unusable reading {sample.get("qty") if isinstance(sample, dict) else sample!r}'
                        if reason not in reasons:
                            reasons.append(reason)
                    continue
                body_samples.setdefault((day, field), []).append(value)
            continue
        if not isinstance(metric, dict) or metric.get("name") not in _DAILY_METRICS:
            continue
        stored = _DAILY_METRICS[metric["name"]]
        units = metric.get("units")
        samples = metric.get("data") or []
        if not isinstance(samples, list) or len(samples) > MAX_SAMPLES_PER_METRIC:
            skipped += 1
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                skipped += 1
                continue
            day = _day(sample.get("date"))
            quantity = {"qty": sample.get("qty"), "units": units}
            value = _kcal(quantity) if stored == "active_kcal" else _quantity(quantity)[0]
            if day is None or value is None or value < 0:
                skipped += 1
                continue
            # Hourly samples add up to the day's total.
            daily[(day, stored)] = daily.get((day, stored), 0.0) + value

    for entry in raw_daily:
        day = _day(entry.get("date")) if isinstance(entry, dict) else None
        if day is None:
            skipped += 1
            continue
        for key, stored in (("active_kcal", "active_kcal"), ("steps", "steps")):
            if entry.get(key) is None:
                continue
            value = _kcal(entry[key]) if stored == "active_kcal" else _quantity(entry[key])[0]
            if value is None or value < 0:
                skipped += 1
            else:
                daily[(day, stored)] = value

    for key, value in daily.items():
        limit = 20000 if key[1] == "active_kcal" else 500000
        daily[key] = round(min(value, limit), 1)
    # Several readings in a day (e.g. two weigh-ins) average out; they are not a total.
    body = {}
    for (day, field), values in body_samples.items():
        body.setdefault(day, {})[field] = round(sum(values) / len(values), 1)

    return {
        "workouts": list(workouts.values()), "daily": daily, "body": body,
        "skipped": skipped, "skipped_reasons": reasons,
        "received": {"workouts": len(raw_workouts), "metrics": [m.get("name") for m in raw_metrics if isinstance(m, dict)][:40]},
    }
