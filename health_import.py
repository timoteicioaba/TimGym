"""Normalize Apple Health exports for storage.

Two payload shapes are accepted:

* Health Auto Export's REST API automation (JSON, version 2 recommended):
  {"data": {"workouts": [...], "metrics": [...]}}
* A simple shape for Shortcuts or scripts:
  {"workouts": [{"id", "name", "start", "duration_min", "active_kcal", "distance_km"}],
   "daily": [{"date", "active_kcal", "steps"}]}

Entries that cannot be read are skipped and counted, not guessed.
"""
from datetime import date, datetime

_KJ_PER_KCAL = 4.184
_KM_PER_UNIT = {"km": 1.0, "m": 0.001, "mi": 1.609344, "yd": 0.0009144, "ft": 0.0003048}
# Health Auto Export metric name -> stored metric name
_DAILY_METRICS = {"active_energy": "active_kcal", "step_count": "steps"}
MAX_ENTRIES = 5000


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


def _timestamp(value):
    """Parse "2026-10-21 18:05:12 +0300" (Health Auto Export) or ISO 8601."""
    text = str(value or "").strip()
    for parse in (
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S %z"),
        lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
    ):
        try:
            return parse(text)
        except ValueError:
            continue
    return None


def _workout(item):
    if not isinstance(item, dict):
        return None
    started = _timestamp(item.get("start"))
    name = str(item.get("name") or item.get("type") or "").strip()[:80]
    if started is None or not name:
        return None

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
    }


def _day(value):
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_health_payload(payload):
    """Return {"workouts": [...], "daily": {(day, metric): value}, "skipped": n}."""
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_workouts = data.get("workouts", [])
    raw_metrics = data.get("metrics", [])
    raw_daily = data.get("daily", [])
    if not all(isinstance(part, list) for part in (raw_workouts, raw_metrics, raw_daily)):
        raise ValueError("workouts, metrics, and daily must be lists.")
    if len(raw_workouts) > MAX_ENTRIES or len(raw_daily) > MAX_ENTRIES:
        raise ValueError(f"Send at most {MAX_ENTRIES} entries at a time.")

    skipped = 0
    workouts = {}
    for item in raw_workouts:
        workout = _workout(item)
        if workout is None:
            skipped += 1
        else:
            workouts[workout["external_id"]] = workout

    daily = {}
    for metric in raw_metrics:
        if not isinstance(metric, dict) or metric.get("name") not in _DAILY_METRICS:
            continue
        stored = _DAILY_METRICS[metric["name"]]
        units = metric.get("units")
        samples = metric.get("data") or []
        if not isinstance(samples, list) or len(samples) > MAX_ENTRIES:
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
    return {"workouts": list(workouts.values()), "daily": daily, "skipped": skipped}
