"""Name a session (Leg day, Push day, Pull day, ...) from the muscles it trained.

Each exercise contributes its number of sets, shared equally among its primary
muscles (from the exercise library). The shares of the muscle groups decide the
label; a session with no known muscles gets no label rather than a guess.
"""

GROUPS = {
    "legs": {"quadriceps", "hamstrings", "glutes", "calves", "adductors", "abductors"},
    "chest": {"chest"},
    "back": {"lats", "middle back", "lower back", "traps"},
    "shoulders": {"shoulders", "neck"},
    "arms": {"biceps", "triceps", "forearms"},
    "core": {"abdominals"},
}
GROUP_LABELS = {"legs": "Legs", "chest": "Chest", "back": "Back", "shoulders": "Shoulders", "arms": "Arms", "core": "Core"}
_GROUP_OF = {muscle: group for group, muscles in GROUPS.items() for muscle in muscles}


def muscle_sets(items):
    """Sets per muscle from (primary_muscles, sets) pairs; unknown muscles are ignored."""
    totals = {}
    for muscles, sets in items:
        known = [muscle for muscle in muscles or [] if muscle in _GROUP_OF]
        for muscle in known:
            totals[muscle] = totals.get(muscle, 0) + sets / len(known)
    return totals


def classify(items):
    """Return {"label": str | None, "groups": [(label, percent), ...]} for a session.

    items: (primary_muscles, sets) for each exercise.
    """
    per_muscle = muscle_sets(items)
    total = sum(per_muscle.values())
    if not total:
        return {"label": None, "groups": []}
    group_sets = {}
    for muscle, sets in per_muscle.items():
        group_sets[_GROUP_OF[muscle]] = group_sets.get(_GROUP_OF[muscle], 0) + sets
    share = {group: sets / total for group, sets in group_sets.items()}
    groups = sorted(((GROUP_LABELS[group], round(100 * value)) for group, value in share.items()), key=lambda item: -item[1])

    legs, core = share.get("legs", 0), share.get("core", 0)
    biceps_and_forearms = per_muscle.get("biceps", 0) + per_muscle.get("forearms", 0)
    push = share.get("chest", 0) + share.get("shoulders", 0) + per_muscle.get("triceps", 0) / total
    pull = share.get("back", 0) + biceps_and_forearms / total
    upper = push + pull

    if legs >= 0.65:
        label = "Leg day"
    elif core >= 0.6:
        label = "Core day"
    elif legs >= 0.3 and upper >= 0.3:
        label = "Full body"
    elif share.get("arms", 0) >= 0.6:
        label = "Arm day"
    elif upper and push / upper >= 0.7:
        if share.get("chest", 0) / push >= 0.6:
            label = "Chest day"
        elif share.get("shoulders", 0) / push >= 0.6:
            label = "Shoulder day"
        else:
            label = "Push day"
    elif upper and pull / upper >= 0.7:
        label = "Back day" if share.get("back", 0) / pull >= 0.9 else "Pull day"
    elif upper:
        label = "Upper body"
    else:
        label = "Leg day" if legs >= core else "Core day"
    return {"label": label, "groups": groups}
