"""Fast, deterministic workout-note parser for common gym logging language.

It deliberately asks for clarification when the note cannot be mapped to sets,
reps, and weights with reasonable confidence instead of guessing.
"""
import re
from datetime import date, timedelta


_WEIGHT_RE = re.compile(
    r"(?<![\w.])(?P<marker>@|at\s+|with\s+)?(?P<sign>[+-]?)\s*"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>kg|kgs|kilograms?|lb|lbs|pounds?)\b",
    re.IGNORECASE,
)
_AT_WEIGHT_RE = re.compile(
    r"@\s*(?P<amount>\d+(?:[.,]\d+)?)(?!\s*(?:reps?|sets?))",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_COMPOSITE_PATTERNS = [
    re.compile(r"(?<!\w)(?P<sets>\d{1,2})\s*sets?\s*(?:(?:of\s*)|[x×]\s*)?(?P<reps>\d{1,3})(?:\s*reps?\b)?", re.IGNORECASE),
    re.compile(r"(?<!\w)(?P<sets>\d{1,2})\s*sets?\s*,\s*(?P<reps>\d{1,3})\s*reps?\b", re.IGNORECASE),
    re.compile(r"(?<!\w)(?P<reps>\d{1,3})\s*reps?\s*(?:for\s*)?(?P<sets>\d{1,2})\s*sets?\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])(?P<weight>\d+(?:[.,]\d+)?\s*(?:kg|kgs|kilograms?|lb|lbs|pounds?))\s*[x×]\s*(?P<reps>\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<!\w)(?P<reps>\d{1,3})\s*[x×]\s*(?P<weight>\d+(?:[.,]\d+)?\s*(?:kg|kgs|kilograms?|lb|lbs|pounds?))", re.IGNORECASE),
    re.compile(r"(?<!\w)(?P<sets>\d{1,2})\s*[x×]\s*(?P<reps>\d{1,3})(?:\s*reps?)?(?!\d)", re.IGNORECASE),
    re.compile(r"(?<!\w)set\s*\d+\s*[:=-]\s*(?P<reps>\d{1,3})\s*reps?\b", re.IGNORECASE),
]
_SINGLE_REPS_RE = re.compile(r"(?<!\w)(?P<reps>\d{1,3})\s*reps?\b", re.IGNORECASE)
_SET_COUNT_RE = re.compile(r"(?<!\w)(?P<sets>\d{1,2})\s+sets?\b", re.IGNORECASE)
_REP_RANGE_RE = re.compile(r"(?<!\w)(?P<sets>\d{1,2})\s*(?:sets?\s*(?:of\s*)?|[x×]\s*)(?P<low>\d{1,3})\s*[-–]\s*(?P<high>\d{1,3})(?:\s*reps?\b)?", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"\b(?:i|did|do|today|yesterday|then|and|after|that|for|of|at|with|"
    r"set|sets|rep|reps|each|my|workout|session|please|log|weight|many|few|some|felt|feeling|heavy|light|easy|hard|tough|minute|minutes|mins|second|seconds|secs|hour|hours|"
    r"kg|kgs|kilogram|kilograms|lb|lbs|pound|pounds|bodyweight|body-weight)\b",
    re.IGNORECASE,
)


def _split_segments(text):
    text = text.replace("\r", "\n")
    text = re.sub(r"(?i)\b(?:and\s+then|then|after\s+that)\b", ";", text)
    text = re.sub(r"\n+|;", ";", text)
    # A comma before another exercise clause is a common way to list sessions.
    text = re.sub(
        r",\s*(?=[A-Za-z][^,;]{0,100}\b(?:\d{1,2}\s*[x×]|\d{1,2}\s+sets?\b))",
        ";",
        text,
        flags=re.IGNORECASE,
    )
    segments = []
    for clause in text.split(";"):
        pieces = re.split(r"\s+and\s+", clause, flags=re.IGNORECASE)
        current = pieces[0].strip()
        for piece in pieces[1:]:
            piece = piece.strip()
            begins_with_exercise = bool(re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ]", piece))
            if begins_with_exercise and _find_prescriptions(current) and _find_prescriptions(piece):
                segments.append(current)
                current = piece
            else:
                current = (current + " and " + piece).strip()
        if current:
            # Carry set-by-set fragments forward as additional prescriptions
            # for the exercise named in the preceding clause.
            if segments and re.match(r"(?i)^(?:set\s*\d+\s*[:=-]\s*)?\d{1,3}\s*reps?\b", current):
                segments[-1] += "; " + current
            else:
                segments.append(current)
    return [part.strip(" ,.") for part in segments if part.strip(" ,.")]


def _find_prescriptions(segment):
    found = []
    for pattern in _COMPOSITE_PATTERNS:
        for match in pattern.finditer(segment):
            if any(match.start() < item["end"] and match.end() > item["start"] for item in found):
                continue
            groups = match.groupdict()
            found.append({
                "start": match.start(),
                "end": match.end(),
                "sets": int(groups.get("sets") or 1),
                "reps": int(groups["reps"]),
            })
    found.sort(key=lambda item: item["start"])

    # A phrase like “5 reps at 80 kg” explicitly describes one set.
    if not found:
        for match in _SINGLE_REPS_RE.finditer(segment):
            found.append({
                "start": match.start(),
                "end": match.end(),
                "sets": 1,
                "reps": int(match.group("reps")),
            })
    return found


def _weight_matches(segment):
    matches = list(_WEIGHT_RE.finditer(segment))
    unit_spans = [(m.start(), m.end()) for m in matches]
    for match in _AT_WEIGHT_RE.finditer(segment):
        if any(match.start() < end and match.end() > start for start, end in unit_spans):
            continue
        matches.append(match)
    return sorted(matches, key=lambda match: match.start())


def _weight_kg(match):
    amount = float(match.group("amount").replace(",", "."))
    unit = (match.groupdict().get("unit") or "kg").lower()
    if unit in {"lb", "lbs", "pound", "pounds"}:
        amount *= 0.45359237
    return round(amount, 2)


def _exercise_name(segment, prescriptions, weights):
    chars = list(segment)
    spans = [(item["start"], item["end"]) for item in prescriptions]
    spans.extend((item.start(), item.end()) for item in weights)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = " "
    name = "".join(chars)
    name = _DATE_RE.sub(" ", name)
    name = re.sub(r"\b(?:date|on)\b", " ", name, flags=re.IGNORECASE)
    name = _NOISE_RE.sub(" ", name)
    name = re.sub(r"\b\d+(?:[.,]\d+)?\b|[@+×x]", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9' -]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -'")
    name = re.sub(r"^(?:i did|did|do|today)\s+", "", name, flags=re.IGNORECASE)
    return name[:80].strip()


def parse_workout_note(note):
    """Return a workout draft, retaining identifiable exercises with missing details."""
    text = str(note or "").strip()
    if not text:
        return {"clarification": "Write your workout first."}
    if len(text) > 4000:
        return {"clarification": "Keep the workout note under 4,000 characters."}

    workout_date = date.today()
    date_match = _DATE_RE.search(text)
    if date_match:
        try:
            workout_date = date.fromisoformat(date_match.group())
        except ValueError:
            return {"clarification": "That date is invalid. Use YYYY-MM-DD."}
        text = text[:date_match.start()] + " " + text[date_match.end():]
    elif re.search(r"\byesterday\b", text, re.IGNORECASE):
        workout_date -= timedelta(days=1)

    parsed = []
    for segment in _split_segments(text):
        prescriptions = _find_prescriptions(segment)
        weights = _weight_matches(segment)
        negative_weight = any(match.groupdict().get("sign") == "-" for match in weights)
        usable_weights = [] if negative_weight else weights
        rep_range = _REP_RANGE_RE.search(segment)
        set_count_match = _SET_COUNT_RE.search(segment)

        if rep_range:
            set_count = int(rep_range.group("sets"))
            prescription_spans = [{
                "start": rep_range.start(), "end": rep_range.end(),
                "sets": set_count, "reps": None,
            }]
            prescriptions_for_name = prescription_spans
            range_weight = min(usable_weights, key=lambda item: abs(item.start() - rep_range.end()), default=None)
            rows = [
                {"reps": None, "weight_kg": _weight_kg(range_weight) if range_weight else None}
                for _ in range(set_count)
            ]
            missing_fields = ["reps"]
        elif prescriptions:
            prescriptions_for_name = prescriptions
            rows = []
            missing_fields = []
            for prescription in prescriptions:
                weight_match = min(
                    usable_weights,
                    key=lambda item: abs(item.start() - prescription["end"]),
                    default=None,
                )
                weight = _weight_kg(weight_match) if weight_match else None
                rows.extend(
                    {"reps": prescription["reps"], "weight_kg": weight}
                    for _ in range(prescription["sets"])
                )
        elif set_count_match:
            set_count = int(set_count_match.group("sets"))
            prescriptions_for_name = [{
                "start": set_count_match.start(), "end": set_count_match.end(),
                "sets": set_count, "reps": None,
            }]
            set_weight = min(usable_weights, key=lambda item: abs(item.start() - set_count_match.end()), default=None)
            rows = [
                {"reps": None, "weight_kg": _weight_kg(set_weight) if set_weight else None}
                for _ in range(set_count)
            ]
            missing_fields = ["reps"]
        else:
            prescriptions_for_name = []
            rows = []
            missing_fields = ["sets", "reps"]

        if len(rows) > 99:
            continue
        if not rows and "sets" not in missing_fields:
            missing_fields.append("sets")
        if negative_weight:
            missing_fields.append("weight_kg")

        name = _exercise_name(segment, prescriptions_for_name, weights)
        if not name:
            continue
        default_weight = _weight_kg(usable_weights[0]) if not rows and len(usable_weights) == 1 else None
        parsed.append({
            "name": name[0].upper() + name[1:],
            "default_weight_kg": default_weight,
            "notes": (
                "Enter a positive load or leave it blank for an assisted exercise."
                if negative_weight else ""
            ),
            "sets": rows,
            "missing_fields": missing_fields,
        })

    if len(parsed) > 30:
        return {"clarification": "Log up to 30 exercises at a time."}
    if not parsed:
        return {"clarification": "I couldn’t identify an exercise. Add its name and any details you know."}

    return {
        "workout": {
            "date": workout_date.isoformat(),
            "exercises": parsed,
        }
    }
