"""Fast, deterministic workout-note parser for common gym logging language.

It deliberately asks for clarification when the note cannot be mapped to sets,
reps, and weights with reasonable confidence instead of guessing.
"""
import re
from datetime import date, timedelta


_WEIGHT_RE = re.compile(
    r"(?<![\\w.])(?P<marker>@|at\\s+|with\\s+)?(?P<sign>[+-]?)\\s*"
    r"(?P<amount>\\d+(?:[.,]\\d+)?)\\s*"
    r"(?P<unit>kg|kgs|kilograms?|lb|lbs|pounds?)\\b",
    re.IGNORECASE,
)
_AT_WEIGHT_RE = re.compile(
    r"@\\s*(?P<amount>\\d+(?:[.,]\\d+)?)(?!\\s*(?:reps?|sets?))",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\\b\\d{4}-\\d{2}-\\d{2}\\b")
_COMPOSITE_PATTERNS = [
    re.compile(r"(?<!\\w)(?P<sets>\\d{1,2})\\s*sets?\\s*(?:of\\s*)?(?P<reps>\\d{1,3})\\s*reps?\\b", re.IGNORECASE),
    re.compile(r"(?<!\\w)(?P<sets>\\d{1,2})\\s*sets?\\s*,\\s*(?P<reps>\\d{1,3})\\s*reps?\\b", re.IGNORECASE),
    re.compile(r"(?<!\\w)(?P<reps>\\d{1,3})\\s*reps?\\s*(?:for\\s*)?(?P<sets>\\d{1,2})\\s*sets?\\b", re.IGNORECASE),
    re.compile(r"(?<!\\w)(?P<sets>\\d{1,2})\\s*[x×]\\s*(?P<reps>\\d{1,3})(?:\\s*reps?)?(?!\\d)", re.IGNORECASE),
    re.compile(r"(?<!\\w)set\\s*\\d+\\s*[:=-]\\s*(?P<reps>\\d{1,3})\\s*reps?\\b", re.IGNORECASE),
]
_SINGLE_REPS_RE = re.compile(r"(?<!\\w)(?P<reps>\\d{1,3})\\s*reps?\\b", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"\\b(?:i|did|do|today|yesterday|then|and|after|that|for|of|at|with|"
    r"set|sets|rep|reps|each|my|workout|session|please|log|weight|"
    r"kg|kgs|kilogram|kilograms|lb|lbs|pound|pounds|bodyweight|body-weight)\\b",
    re.IGNORECASE,
)


def _split_segments(text):
    text = text.replace("\\r", "\\n")
    text = re.sub(r"(?i)\\b(?:and\\s+then|then|after\\s+that)\\b", ";", text)
    text = re.sub(r"\\n+|;", ";", text)
    # A comma before another exercise clause is a common way to list sessions.
    text = re.sub(
        r",\\s*(?=[A-Za-z][^,;]{0,100}\\b(?:\\d{1,2}\\s*[x×]|\\d{1,2}\\s+sets?\\b))",
        ";",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\\s+and\\s+(?=[A-Za-z][^;]{0,100}\\b(?:\\d{1,2}\\s*[x×]|\\d{1,2}\\s+sets?\\b))",
        ";",
        text,
        flags=re.IGNORECASE,
    )
    return [part.strip(" ,.") for part in text.split(";") if part.strip(" ,.")]


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
    name = re.sub(r"\\b(?:date|on)\\b", " ", name, flags=re.IGNORECASE)
    name = _NOISE_RE.sub(" ", name)
    name = re.sub(r"\\b\\d+(?:[.,]\\d+)?\\b|[@+×x]", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9' -]", " ", name)
    name = re.sub(r"\\s+", " ", name).strip(" -'")
    name = re.sub(r"^(?:i did|did|do|today)\\s+", "", name, flags=re.IGNORECASE)
    return name[:80].strip()


def parse_workout_note(note):
    """Return a normalized workout or a clarification message."""
    text = str(note or "").strip()
    if not text:
        return {"clarification": "Write your workout first."}
    if len(text) > 4000:
        return {"clarification": "Keep the workout note under 4,000 characters."}

    workout_date = date.today()
    date_match = _DATE_RE.search(text)
    if date_match:
        try:
            workout_date = date.fromisoformat(date_match.group()).date()
        except ValueError:
            return {"clarification": "That date is invalid. Use YYYY-MM-DD."}
        text = text[:date_match.start()] + " " + text[date_match.end():]
    elif re.search(r"\\byesterday\\b", text, re.IGNORECASE):
        workout_date -= timedelta(days=1)

    parsed = []
    unknown = []
    for segment in _split_segments(text):
        prescriptions = _find_prescriptions(segment)
        if not prescriptions:
            unknown.append(segment)
            continue
        if any(not 1 <= item["sets"] <= 99 or not 1 <= item["reps"] <= 999 for item in prescriptions):
            unknown.append(segment)
            continue
        if any(re.match(r"\\s*[-–]\\s*\\d", segment[item["end"]:]) for item in prescriptions):
            unknown.append(segment)
            continue

        weights = _weight_matches(segment)
        name = _exercise_name(segment, prescriptions, weights)
        if not name:
            unknown.append(segment)
            continue

        set_rows = []
        for prescription in prescriptions:
            weight_match = min(
                weights,
                key=lambda item: abs(item.start() - prescription["end"]),
                default=None,
            )
            weight = _weight_kg(weight_match) if weight_match else None
            set_rows.extend(
                {"reps": prescription["reps"], "weight_kg": weight}
                for _ in range(prescription["sets"])
            )
        parsed.append({"name": name[0].upper() + name[1:], "notes": "", "sets": set_rows})

    if unknown:
        example = "Try “Squat 3x5 @ 80 kg” or “Bench press: 3 sets of 8 at 60 lb”."
        return {"clarification": f"I couldn’t confidently parse: {unknown[0]}. {example}"}
    if not parsed:
        return {"clarification": "Add an exercise name and its sets and reps. Try “Squat 3x5 @ 80 kg”."}
    if len(parsed) > 30:
        return {"clarification": "Log up to 30 exercises at a time."}

    return {"workout": {"date": workout_date.isoformat(), "exercises": parsed}}
