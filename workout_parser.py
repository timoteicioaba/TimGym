"""Fast, deterministic workout-note parser for common gym logging language.

A note is split into clauses (lines, semicolons, commas, "then"), each clause
is tokenized, and the tokens are matched against the shorthand people actually
write in a gym log:

    Squat 3x5 @ 80 kg            Bench 60x8, 70x6, 80x5
    Squat 100x5x3                Deadlift 140 for 5
    OHP 40kg 8,8,7               Bench 5/5/5 @ 80
    Rows 3 sets of 10 at 50      Bench press (new line) 60x10 (new line) 80x8

Lines without an exercise name continue the previous exercise. Words after
the details ("felt heavy") become notes. Unknown details are left empty for
the person to fill in during review rather than guessed.
"""
import re
from datetime import date, timedelta

_LB_TO_KG = 0.45359237
_UNIT = r"kgs?|kilos?|kilograms?|lbs?|pounds?"
_TOKEN_RE = re.compile(
    rf"(?P<range>(?<![\d.,])\d{{1,3}}\s*[-–]\s*\d{{1,3}}(?!\s*(?:[.,]?\d|{_UNIT})))"
    rf"|(?P<num>(?:(?<=^)|(?<=[\s(:@,/]))[+-]?\d+(?:\.\d+|,\d{{1,2}}(?=\s*(?:{_UNIT})\b))?|\d+(?:\.\d+)?)"
    rf"(?:\s*(?P<unit>{_UNIT})\b)?"
    r"|(?P<word>[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*)"
    r"|(?P<at>@)"
    r"|(?P<x>[×*])"
    r"|(?P<sep>[,/&+])",
    re.IGNORECASE,
)
_KEYWORDS = {
    "set": "sets", "sets": "sets",
    "rep": "reps", "reps": "reps", "repetitions": "reps",
    "of": "of", "for": "for", "x": "x",
    "at": "at", "with": "at",
    "rpe": "rpe",
    "bw": "bw", "bodyweight": "bw", "body-weight": "bw",
}
# Words that carry no meaning for the exercise name.
_FILLER = {
    "i", "did", "do", "done", "today", "yesterday", "my", "the", "a", "an", "some",
    "then", "also", "plus", "and", "on", "date", "each", "per", "side",
    "workout", "session", "log", "please", "kg", "kgs", "lb", "lbs", "ago", "days",
    "day", "last", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
_SIDE_WORDS = {"each", "per", "side"}
# A clause that starts with one of these describes the previous exercise.
_NOTE_STARTERS = {
    "felt", "feeling", "feels", "was", "were", "it", "its", "it's", "easy", "hard", "heavy",
    "light", "tough", "good", "great", "bad", "form", "note", "notes", "too", "very", "pretty",
    "super", "slow", "fast", "tempo", "paused", "failed", "struggled", "grind", "grindy", "pr",
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SET_LABEL_RE = re.compile(r"(?i)\bset\s*\d{1,2}\s*[:=.)-]\s*")
_PER_LIMB_RE = re.compile(r"(?i)\b(?:each|per)\s+(?:arm|leg|hand|side)\b")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?i)(\n|;|\.(?=\s+[A-Za-z]|\s*$)|,(?!\d{1,2}\s*(?:kg|lb))|\band\s+then\b|\bafter\s+that\b|\bthen\b|\band\b)"
)


class _Token:
    __slots__ = ("kind", "value", "unit", "sign", "text", "low", "high")

    def __init__(self, kind, text, value=None, unit=None, sign="", low=None, high=None):
        self.kind, self.text, self.value, self.unit, self.sign = kind, text, value, unit, sign
        self.low, self.high = low, high

    def is_num(self, bare=False):
        return self.kind == "num" and (not bare or (not self.unit and not self.sign))

    def is_kw(self, *names):
        return self.kind == "kw" and self.value in names


def _tokenize(text):
    tokens = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0)
        if match.group("range"):
            low, high = (int(part) for part in re.split(r"\s*[-–]\s*", raw))
            tokens.append(_Token("range", raw, low=low, high=high))
        elif match.group("num"):
            number = match.group("num")
            sign = number[0] if number[0] in "+-" else ""
            value = float(number.lstrip("+-").replace(",", "."))
            unit = (match.group("unit") or "").lower()
            unit = "lb" if unit.startswith(("lb", "pound")) else ("kg" if unit else None)
            tokens.append(_Token("num", raw, value=value, unit=unit, sign=sign))
        elif match.group("word"):
            lower = raw.lower()
            if lower in _KEYWORDS:
                tokens.append(_Token("kw", raw, value=_KEYWORDS[lower]))
            elif lower in _FILLER:
                tokens.append(_Token("filler", raw, value=lower))
            else:
                tokens.append(_Token("word", raw))
        elif match.group("at"):
            tokens.append(_Token("kw", raw, value="at"))
        elif match.group("x"):
            tokens.append(_Token("kw", raw, value="x"))
        else:
            tokens.append(_Token("sep", raw))
    return tokens


def _kg(token):
    amount = token.value * _LB_TO_KG if token.unit == "lb" else token.value
    return round(amount, 2)


def _extract_date(text, today):
    """Return (workout_date, text without the date phrase) or raise ValueError."""
    match = _ISO_DATE_RE.search(text)
    if match:
        try:
            found = date.fromisoformat(match.group())
        except ValueError as exc:
            raise ValueError("That date is invalid. Use YYYY-MM-DD.") from exc
        return found, text[:match.start()] + " " + text[match.end():]
    match = re.search(r"(?i)\b(\d{1,2})\s+days?\s+ago\b", text)
    if match:
        return today - timedelta(days=int(match.group(1))), text[:match.start()] + " " + text[match.end():]
    if re.search(r"(?i)\byesterday\b", text):
        return today - timedelta(days=1), text
    match = re.search(r"(?i)\b(?:on\s+|last\s+)?(" + "|".join(_WEEKDAYS) + r")\b", text)
    if match:
        back = (today.weekday() - _WEEKDAYS.index(match.group(1).lower())) % 7
        if back == 0 and match.group(0).lower().startswith("last"):
            back = 7
        return today - timedelta(days=back), text[:match.start()] + " " + text[match.end():]
    return today, text


def _split_clauses(text):
    """Split into (separator, clause) pairs; the separator is what preceded the clause."""
    parts = _CLAUSE_SPLIT_RE.split(text)
    clauses = [("", parts[0])]
    for index in range(1, len(parts), 2):
        clauses.append((parts[index].strip().lower(), parts[index + 1]))
    return [(separator, clause.strip()) for separator, clause in clauses if clause.strip()]


def _describe_clause(tokens):
    """Separate a clause into its exercise name, detail tokens, and trailing notes.

    Also reports whether the name came after the details ("100 for 3x5 squat").
    """
    name_words, details, notes = [], [], []
    name_after_details = None
    state = "before"  # before the name, in the name, after the name
    for token in tokens:
        if token.kind == "word":
            if state == "before" or (state == "name" and not details):
                if name_after_details is None:
                    name_after_details = bool(details)
                name_words.append(token.text)
                state = "name"
            else:
                notes.append(token.text)
                if state == "before":
                    state = "after"
        elif token.kind == "filler":
            if notes:
                notes.append(token.text)
            elif token.value in _SIDE_WORDS and details:
                notes.append(token.text)
        else:
            if state == "name":
                state = "after"
            details.append(token)
    # Hyphen-joined words (e.g. "t-bar") survive tokenizing; strip stray edge hyphens.
    name = " ".join(name_words).strip(" -'’")
    return name, details, " ".join(notes), bool(name_after_details)


def _weight_after(tokens, index):
    """Match a load directly following a set prescription: '@ 80', 'at 80 kg', '80kg'."""
    if index < len(tokens) and tokens[index].is_kw("at") and index + 1 < len(tokens) and tokens[index + 1].is_num():
        # "@ 8 rpe" is an effort rating, not a load.
        if index + 2 < len(tokens) and tokens[index + 2].is_kw("rpe"):
            return None, index
        return tokens[index + 1], index + 2
    if index < len(tokens) and tokens[index].is_num() and tokens[index].unit:
        return tokens[index], index + 1
    return None, index


def _parse_details(tokens):
    """Turn detail tokens into set groups and standalone loads, in order."""
    items = []  # ("group", {...}) or ("weight", token or None)
    groups = []
    pending_rpe = None
    i = 0
    n = len(tokens)

    def at(offset, kind=None, kw=None, bare=False):
        j = i + offset
        if j >= n:
            return None
        token = tokens[j]
        if kw is not None:
            return token if token.is_kw(*((kw,) if isinstance(kw, str) else kw)) else None
        if kind == "num":
            return token if token.is_num(bare) else None
        if kind is not None:
            return token if token.kind == kind else None
        return token

    def add_group(sets, reps, weight=None, reps_list=None, own_weight=False):
        group = {"sets": sets, "reps": reps, "reps_list": reps_list, "weight": weight,
                 "has_weight": own_weight, "rpe": None}
        items.append(("group", group))
        groups.append(group)
        return group

    while i < n:
        token = tokens[i]
        consumed = 0
        group = None

        # Effort ratings: "RPE 8", "8 RPE", "@ 8 RPE".
        if token.is_kw("rpe") and at(1, "num"):
            value, consumed = tokens[i + 1].value, 2
        elif token.is_num(bare=True) and at(1, kw="rpe"):
            value, consumed = token.value, 2
        elif token.is_kw("at") and at(1, "num", bare=True) and at(2, kw="rpe"):
            value, consumed = tokens[i + 1].value, 3
        if consumed:
            if groups and groups[-1]["rpe"] is None:
                groups[-1]["rpe"] = value
            else:
                pending_rpe = value
            i += consumed
            continue

        a, b, c = at(0, "num"), at(2, "num"), at(4, "num")
        # Triple: "3 x 10 x 55" (sets, reps, load) or "100 x 5 x 3" (load, reps, sets).
        if a and at(1, kw="x") and b and at(3, kw="x") and c:
            if a.unit or (not c.unit and a.value > max(b.value, c.value)):
                group = add_group(int(c.value), int(b.value), a, own_weight=True)
            else:
                group = add_group(int(a.value), int(b.value), c, own_weight=True)
            i += 5
        # "3 sets of 8", "3 sets, 10 reps", "3 sets of 8-12", or just "3 sets".
        elif a and at(1, kw="sets"):
            j = i + 2
            if j < n and tokens[j].kind == "sep":
                j += 1
            if j < n and tokens[j].is_kw("of", "x"):
                j += 1
            if j < n and (tokens[j].is_num(bare=True) or tokens[j].kind == "range"):
                reps = int(tokens[j].value) if tokens[j].kind == "num" else None
                j += 1
                if j < n and tokens[j].is_kw("reps"):
                    j += 1
                group = add_group(int(a.value), reps)
                i = j
            else:
                group = add_group(int(a.value), None)
                i += 2
        # "8 reps for 3 sets", "8 reps x 3 sets".
        elif a and at(1, kw="reps") and at(2, kw=("for", "x")) and at(3, "num") and at(4, kw="sets"):
            group = add_group(int(at(3).value), int(a.value))
            i += 5
        # Pairs: "3x5", "60x8", "100kg x 5", "5 x 100kg", "3x8-12".
        elif a and at(1, kw="x") and (b or at(2, "range")):
            if not b:
                group = add_group(int(a.value), None)
            elif a.unit or a.sign:
                group = add_group(1, int(b.value), a, own_weight=True)
            elif b.unit or b.sign:
                group = add_group(1, int(a.value), b, own_weight=True)
            elif a.value > 10 or a.value != int(a.value):
                group = add_group(1, int(b.value), a, own_weight=True)
            else:
                group = add_group(int(a.value), int(b.value))
            i += 3
            if i < n and tokens[i].is_kw("reps"):
                i += 1
        # "140 for 5" is one set; "100 for 3x5" is a load followed by a prescription.
        elif a and at(1, kw="for") and at(2, "num", bare=True) and not at(3, kw=("x", "sets")):
            group = add_group(1, int(tokens[i + 2].value), a, own_weight=True)
            i += 3
            if i < n and tokens[i].is_kw("reps"):
                i += 1
        # "5 reps", "8-12 reps".
        elif (a and not a.unit and at(1, kw="reps")) or (token.kind == "range" and at(1, kw="reps")):
            group = add_group(1, int(a.value) if a else None)
            i += 2
        # "5 @ 100", "5 at 100kg".
        elif a and not a.unit and not a.sign and a.value <= 50 and a.value == int(a.value) \
                and at(1, kw="at") and at(2, "num") and not at(3, kw="rpe"):
            group = add_group(1, int(a.value), tokens[i + 2], own_weight=True)
            i += 3
        # Rep lists: "8,8,7", "5/5/5".
        elif a and a.is_num(bare=True) and a.value <= 50 and a.value == int(a.value) \
                and at(1, "sep") and at(2, "num", bare=True):
            reps_list = [int(a.value)]
            j = i + 1
            while j + 1 < n and tokens[j].kind == "sep" and tokens[j + 1].is_num(bare=True) \
                    and tokens[j + 1].value <= 50 and tokens[j + 1].value == int(tokens[j + 1].value):
                reps_list.append(int(tokens[j + 1].value))
                j += 2
            if j < n and tokens[j].is_kw("reps"):
                j += 1
            group = add_group(len(reps_list), None, reps_list=reps_list)
            i = j
        # Loads on their own: "@ 80", "at 80", "with 70kg", "80kg", "bodyweight", "100".
        elif token.is_kw("at") and at(1, "num"):
            items.append(("weight", tokens[i + 1]))
            i += 2
        elif token.is_kw("bw"):
            items.append(("weight", None))
            i += 1
        elif a:
            items.append(("weight", a))
            i += 1
        else:
            i += 1

        if group is not None:
            if not group["has_weight"]:
                weight, i = _weight_after(tokens, i)
                if weight is not None:
                    group["weight"], group["has_weight"] = weight, True
            if pending_rpe is not None:
                group["rpe"], pending_rpe = pending_rpe, None
    return items, groups


def _resolve_loads(items):
    """Give each group without its own load the nearest standalone load:
    the latest one before it, otherwise the first one after it."""
    standalone = [(index, item) for index, (kind, item) in enumerate(items) if kind == "weight"]
    for index, (kind, group) in enumerate(items):
        if kind != "group" or group["has_weight"]:
            continue
        before = [w for position, w in standalone if position < index]
        after = [w for position, w in standalone if position > index]
        if before:
            group["weight"], group["has_weight"] = before[-1], True
        elif after:
            group["weight"], group["has_weight"] = after[0], True


def _build_exercise(name, tokens, notes):
    items, groups = _parse_details(tokens)
    _resolve_loads(items)
    standalone = [item for kind, item in items if kind == "weight" and item is not None]
    negative = any(token.is_num() and token.sign == "-" for token in tokens)

    sets = []
    for group in groups:
        weight = group["weight"]
        weight_kg = None if weight is None or weight.sign == "-" else _kg(weight)
        reps_values = group["reps_list"] or [group["reps"]] * group["sets"]
        sets.extend({"reps": reps, "weight_kg": weight_kg, "rpe": group["rpe"]} for reps in reps_values)

    default_weight = None
    if not sets:
        load = next((token for token in standalone if token.sign != "-"), None)
        default_weight = _kg(load) if load is not None else None
        # Give the person one row to fill in rather than an empty exercise.
        sets = [{"reps": None, "weight_kg": default_weight, "rpe": None}]

    missing = []
    if not groups:
        missing.append("sets")
    if any(one["reps"] is None for one in sets):
        missing.append("reps")
    if negative:
        missing.append("weight_kg")
        note = "Enter a positive load or leave it blank for an assisted exercise."
        notes = f"{notes} · {note}" if notes else note
    return {
        "name": name[0].upper() + name[1:],
        "default_weight_kg": default_weight,
        "notes": notes[:500],
        "sets": sets,
        "missing_fields": missing,
    }


def _name_key(name):
    words = re.sub(r"[^a-z0-9 ]", " ", name.casefold()).split()
    return " ".join(word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words)


def parse_workout_note(note, known_exercises=(), today=None):
    """Return a workout draft, retaining identifiable exercises with missing details.

    known_exercises: names previously logged; a matching name ("squats" for
    "Squat") reuses the stored spelling so history and charts stay consistent.
    """
    text = str(note or "").strip()
    if not text:
        return {"clarification": "Write your workout first."}
    if len(text) > 4000:
        return {"clarification": "Keep the workout note under 4,000 characters."}

    try:
        workout_date, text = _extract_date(text, today or date.today())
    except ValueError as exc:
        return {"clarification": str(exc)}
    text = _SET_LABEL_RE.sub(" ", text)
    text = _PER_LIMB_RE.sub(" each ", text)

    exercises = []  # each: {"name", "tokens", "notes"}
    orphan_details = []
    for separator, clause in _split_clauses(text):
        tokens = _tokenize(clause)
        name, details, notes, name_after_details = _describe_clause(tokens)
        first_word = next((token.text.lower() for token in tokens if token.kind in ("word", "filler")), "")
        if name and name_after_details and exercises and (first_word in _NOTE_STARTERS or separator == ","):
            # "Bench 60x8, 80x5 felt strong": the words describe the sets, not a new exercise.
            notes, name = " ".join(part for part in (name, notes) if part), ""
        if not name and not details:
            if notes and exercises:
                exercises[-1]["notes"].append(notes)
            continue
        if not name:
            # Details on their own continue the previous exercise ("60x10" on a new line).
            target = exercises[-1]["tokens"] if exercises else orphan_details
            target.append(_Token("sep", ","))
            target.extend(details)
            if notes and exercises:
                exercises[-1]["notes"].append(notes)
            continue
        if not details and exercises and first_word in _NOTE_STARTERS:
            exercises[-1]["notes"].append(" ".join(token.text for token in tokens if token.kind != "sep"))
            continue
        if separator == "and" and exercises and not exercises[-1]["tokens"]:
            # "Clean and jerk 3x2": a bare name joined by "and" is one exercise.
            exercises[-1]["name"] += " and " + name
            exercises[-1]["tokens"].extend(details)
            if notes:
                exercises[-1]["notes"].append(notes)
            continue
        if separator == "and" and exercises and not details:
            # "Squat 3x5 and felt fine" style tails are notes, not exercises.
            exercises[-1]["notes"].append(" ".join(token.text for token in tokens if token.kind != "sep"))
            continue
        exercises.append({"name": name, "tokens": orphan_details + details, "notes": [notes] if notes else []})
        orphan_details = []

    if len(exercises) > 30:
        return {"clarification": "Log up to 30 exercises at a time."}
    if not exercises:
        return {"clarification": "I couldn’t identify an exercise. Add its name and any details you know."}

    known = {_name_key(name): name for name in known_exercises if name}
    parsed = []
    for exercise in exercises:
        name = exercise["name"][:80].strip()
        name = known.get(_name_key(name), name)
        built = _build_exercise(name, exercise["tokens"], " ".join(exercise["notes"]).strip())
        if len(built["sets"]) > 99:
            return {"clarification": f"{built['name']} has more than 99 sets. Check the numbers."}
        parsed.append(built)

    return {
        "workout": {
            "date": workout_date.isoformat(),
            "exercises": parsed,
        }
    }
