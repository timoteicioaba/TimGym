# TimGym

A self-hosted personal workout and body-measurement tracker. Each account has private data.

## Deploy or update on CasaOS

From the folder containing `compose.yaml`, run:

```sh
docker compose up -d --build --remove-orphans
```

Open `https://gym.tim0tei.fun` (or your server address). Workout interpretation runs locally in the app and does not download a model.

SQLite data is stored in the `gym_data` Docker volume and survives container restarts.

## Create accounts

There is no public signup. Create each account from the CasaOS terminal:

```sh
docker compose exec timgym python app.py create-user <username>
```

Enter and confirm a password of at least 12 characters when prompted. Create one account for each person. Existing workouts and measurements from the first version are assigned to the first account you create; each later account sees only its own records.

To list accounts:

```sh
docker compose exec timgym python app.py list-users
```

## Log workouts with the local parser

Type a workout on the dashboard the way you would in a notes app and choose **Parse workout** (or press Ctrl/⌘+Enter). TimGym understands common gym shorthand:

| You write | TimGym logs |
| --- | --- |
| `Squat 3x5 @ 100` · `squat 3 sets of 5 at 100kg` | 3 sets of 5 at 100 kg |
| `Bench 60x8, 70x6, 80x5` · `Squat: 100kg x 5, 110kg x 3` | one set per weight × reps pair |
| `Squat 100x5x3` · `Lat pulldown 3x10x55` | 3 sets of 5 at 100 kg; 3 sets of 10 at 55 kg |
| `OHP 40kg 8,8,7` · `Bench 5/5/5 @ 80` | one set per listed rep count |
| `Deadlift 140 for 5` · `Squat 5 @ 100, 3 @ 110` | single sets |
| `Bench 3x8 RPE 8 70kg` · `Dips 3x12 +20kg` · `Pull ups 3x10` | RPE, added weight, and bodyweight sets |

Put one exercise per line, or separate them with commas, semicolons, or “then”. A line with only numbers (for example `80x8` under `Bench press`) continues the exercise above it, and words after the numbers (“felt heavy”) become notes. Numbers without a unit are kilograms; pounds (`lb`) are converted. Dates such as `2026-10-21`, `yesterday`, `3 days ago`, or `on monday` set the workout date.

Everything in the preview is editable before you save: the date, exercise names (suggested from your history), reps, weight, RPE, and notes. You can add or remove sets and exercises. Anything the parser could not tell is left blank and highlighted for you to fill in. A name you have logged before, such as `squats` for an existing `Squat`, reuses the saved spelling so your history and charts stay consistent.

Parsing runs in the TimGym app itself. Workout notes are not sent to an AI service, no model is downloaded, and there is no extra API cost.

Run the parser tests with `python -m unittest discover -s tests`.

The iPhone Shortcut in [SHORTCUT.md](SHORTCUT.md) remains an optional alternative. Each person using it needs their own private API key.

## Optional: connect a custom GPT Action

You can also connect a custom GPT to the workout API using the OpenAPI schema at `https://gym.tim0tei.fun/openapi.yaml`. Each person's GPT should use their own TimGym key. See OpenAI's [GPT Actions guide](https://developers.openai.com/api/docs/actions/introduction).

The API key grants access only to that person's workouts. Rotating it in TimGym invalidates the old key.

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not share account passwords or TimGym API keys.
