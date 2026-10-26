"""Exercise catalog: names, instructions, muscles, and photos for ~870 exercises.

Data comes from Free Exercise DB (public domain; see catalog/SOURCE.md). match()
turns gym shorthand ("bench", "OHP", "RDL", "pull ups") into catalog entries.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalog") / "exercises.json"
IMAGE_SOURCE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

# Shorthand people log -> the catalog entry they almost always mean.
ALIASES = {
    "bench": "Barbell Bench Press - Medium Grip", "bench press": "Barbell Bench Press - Medium Grip",
    "flat bench": "Barbell Bench Press - Medium Grip", "bp": "Barbell Bench Press - Medium Grip",
    "incline bench": "Barbell Incline Bench Press - Medium Grip", "incline bench press": "Barbell Incline Bench Press - Medium Grip",
    "incline db press": "Incline Dumbbell Press", "incline dumbbell press": "Incline Dumbbell Press",
    "db bench": "Dumbbell Bench Press", "dumbbell bench": "Dumbbell Bench Press",
    "squat": "Barbell Squat", "back squat": "Barbell Squat", "front squat": "Front Barbell Squat",
    "goblet squat": "Goblet Squat",
    "deadlift": "Barbell Deadlift", "dl": "Barbell Deadlift", "rdl": "Romanian Deadlift",
    "ohp": "Standing Military Press", "overhead press": "Standing Military Press", "military press": "Standing Military Press",
    "press": "Standing Military Press", "shoulder press": "Dumbbell Shoulder Press", "db shoulder press": "Dumbbell Shoulder Press",
    "pullup": "Pullups", "chinup": "Chin-Up", "pushup": "Pushups", "dip": "Parallel Bar Dip",
    "row": "Bent Over Barbell Row", "barbell row": "Bent Over Barbell Row", "bent over row": "Bent Over Barbell Row",
    "cable row": "Seated Cable Rows", "seated row": "Seated Cable Rows",
    "lat pulldown": "Wide-Grip Lat Pulldown", "pulldown": "Wide-Grip Lat Pulldown",
    "leg press": "Leg Press", "hip thrust": "Barbell Hip Thrust",
    "curl": "Barbell Curl", "bicep curl": "Barbell Curl", "barbell curl": "Barbell Curl",
    "db curl": "Dumbbell Bicep Curl", "dumbbell curl": "Dumbbell Bicep Curl", "hammer curl": "Hammer Curls",
    "leg curl": "Lying Leg Curls", "leg extension": "Leg Extensions", "calf raise": "Standing Calf Raises",
    "lateral raise": "Side Lateral Raise", "side raise": "Side Lateral Raise", "face pull": "Face Pull",
    "tricep pushdown": "Triceps Pushdown", "triceps pushdown": "Triceps Pushdown", "pushdown": "Triceps Pushdown",
    "lunge": "Dumbbell Lunges", "plank": "Plank", "shrug": "Barbell Shrug",
    "skull crusher": "EZ-Bar Skullcrusher", "skullcrusher": "EZ-Bar Skullcrusher",
    "hanging leg raise": "Hanging Leg Raise", "cable fly": "Cable Crossover", "cable crossover": "Cable Crossover",
}
_JOINED = {"pull up": "pullup", "chin up": "chinup", "push up": "pushup", "sit up": "situp"}
_EXPAND = {"db": "dumbbell", "bb": "barbell", "kb": "kettlebell", "tricep": "triceps", "bicep": "biceps"}
_STRENGTH = {"strength", "powerlifting", "olympic weightlifting"}


def normalize(name):
    """"Pull-Ups" -> "pullup", "Triceps Pushdowns" -> "triceps pushdown"."""
    text = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").casefold().replace("-", " "))
    text = " ".join(text.split())
    for phrase, joined in _JOINED.items():
        text = re.sub(rf"\b{phrase}s?\b", joined, text)
    words = [word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word for word in text.split()]
    return " ".join(words)


@lru_cache(maxsize=1)
def _catalog():
    entries = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for entry in entries:
        entry["_key"] = normalize(entry["name"])
        entry["_tokens"] = set(entry["_key"].split())
    by_id = {entry["id"]: entry for entry in entries}
    aliases = {}
    by_name = {entry["name"]: entry for entry in entries}
    for alias, target in ALIASES.items():
        if target in by_name:
            aliases[normalize(alias)] = by_name[target]
            by_name[target]["_popular"] = True
    return entries, by_id, aliases


def get(exercise_id):
    return _catalog()[1].get(exercise_id)


def image_path(entry, index=0):
    """The dataset-relative path of an entry's photo, e.g. "Barbell_Squat/0.jpg"."""
    images = entry.get("images") or []
    return images[index] if index < len(images) else None


def summary(entry):
    """The public fields of an entry, for JSON responses and templates."""
    return {
        "id": entry["id"], "name": entry["name"], "equipment": entry.get("equipment"),
        "muscles": entry.get("primaryMuscles") or [], "images": len(entry.get("images") or []),
    }


def _score(query_tokens, entry):
    tokens = entry["_tokens"]
    matched = sum(1 for q in query_tokens if q in tokens or (len(q) >= 3 and any(t.startswith(q) for t in tokens)))
    if not matched:
        return None
    coverage = matched / len(query_tokens)
    extra = len(tokens) - matched
    return (coverage * 10 - extra * 0.6 + (1 if entry.get("category") in _STRENGTH else 0) + (2 if coverage == 1 else 0)
            + (1.5 if entry.get("_popular") else 0))


def search(query, limit=8):
    """Catalog entries for a typed name, best first."""
    key = normalize(query)
    if not key:
        return []
    entries, _by_id, aliases = _catalog()
    expanded = " ".join(_EXPAND.get(word, word) for word in key.split())
    query_tokens = expanded.split()
    scored = []
    for entry in entries:
        score = _score(query_tokens, entry)
        if score is not None:
            if entry["_key"] in (key, expanded):
                score += 20
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], len(item[1]["name"])))
    results = [entry for _score_value, entry in scored]
    alias = aliases.get(key) or aliases.get(expanded)
    if alias:
        results = [alias] + [entry for entry in results if entry is not alias]
    return results[:limit]


@lru_cache(maxsize=4096)
def match(name):
    """The catalog entry a logged name confidently refers to, or None."""
    key = normalize(name)
    if not key:
        return None
    entries, _by_id, aliases = _catalog()
    expanded = " ".join(_EXPAND.get(word, word) for word in key.split())
    if key in aliases or expanded in aliases:
        return aliases.get(key) or aliases.get(expanded)
    exact = next((entry for entry in entries if entry["_key"] in (key, expanded)), None)
    if exact:
        return exact
    # Otherwise accept the best search hit only if it contains every word typed.
    best = search(name, limit=1)
    if best:
        query_tokens = set(expanded.split())
        if query_tokens <= best[0]["_tokens"] and len(best[0]["_tokens"] - query_tokens) <= 3:
            return best[0]
    return None
