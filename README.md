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

To delete the data an account entered outside Apple Health (gym workouts logged in TimGym, including through the API, and body check-ins entered by hand), first back up the database, then run:

```sh
docker compose cp timgym:/data/timgym.db ./timgym-backup.db
docker compose exec timgym python app.py delete-manual-data <username>
```

It lists what will be deleted and asks you to type the username to confirm. Apple Health workouts, activity, nutrition, imported check-ins, the account, and its API key are kept.

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

Run the tests with `python -m unittest discover -s tests`.

The iPhone Shortcut in [SHORTCUT.md](SHORTCUT.md) remains an optional alternative. Each person using it needs their own private API key.

## Use it like an app

TimGym is installable as a home-screen app. On iPhone, open the site in Safari, tap **Share → Add to Home Screen**, and sign in once inside the installed app. The app has four tabs:

- **Today** has three sections. **Workouts** shows the week, days trained, training time, recent sessions, and recent exercises with your latest set, best, and change; tap an exercise for its history. **Nutrition** shows today's calories eaten and burned, steps, the protein/carbs/fat split, and the last 7 days of intake. **Body** shows your latest weight, body fat, waist, and a 30-day trend. When Apple Health recorded a strength workout today that you haven't logged, a prompt at the top takes you to Log for that session.
- **Log** is where you type workouts. Tap the chips with your recent exercise names to start a line, and add body check-ins.
- **Progress** has the main-lift charts, body weight, and imported Apple Health workouts.
- **Settings** has Apple Health sync, your API key, and sign-out.

Set `TIMEZONE` (for example `TIMEZONE=Europe/Bucharest` in your environment or `compose.yaml`) so “today” and the week view follow your local day instead of UTC.

## Sync Apple Health

Websites can't read Apple Health directly, so an iPhone app sends the data to TimGym. The tested route is [Health Auto Export](https://apps.apple.com/app/id1115567069). Its automations need the app's premium tier.

1. In TimGym, open **Settings** and generate your API key.
2. In Health Auto Export, create two **Automations** of type **REST API**, both with:
   - URL: `https://gym.tim0tei.fun/api/health`
   - Header: `Authorization: Bearer YOUR_TIMGYM_KEY`
   - Format **JSON**, export version **2**, aggregation **Days**

   The **Workouts** automation sends your Apple Health workouts. The **Health metrics** automation sends **Active Energy**, **Step Count**, **Weight**, **Body Fat Percentage**, **Waist Circumference**, and for nutrition **Dietary Energy**, **Protein**, **Carbohydrates**, and **Total Fat**. Food apps such as MyFitnessPal, Lose It!, or Cronometer write those to Apple Health. If a metric arrives under a name TimGym does not recognize, **Settings → Recent imports** lists it as “received but not used”.
3. Run it once manually with a short range, such as the last 7 days. **Settings → Apple Health** shows how many workouts and days arrived. Uploads are limited to 50 MB, so sync long histories in parts.

If Health Auto Export only reports “error”, run `docker compose logs --tail=20 timgym`. Every refused import is logged with its reason (for example an unknown API key, a body that is not JSON, or an upload over the limit). The JSON response also carries the reason in `error`.

**Settings → Apple Health → Recent imports** shows what each sync contained and why any entry was skipped.

Workouts are matched by their Apple Health ID and daily totals replace earlier values, so syncing the same days again never double-counts. Body readings become one check-in per day on your weight chart, averaged if you weighed in more than once and updated on re-sync. A check-in you enter in TimGym for the same day takes precedence. Pounds and inches are converted.

A strength-type Apple Health workout (traditional or functional strength, core, cross training, HIIT) and the exercises you log in TimGym on the same day are shown as one session, with Apple's time and calories next to your sets. Cardio such as runs or elliptical stays a separate entry. If you do two strength sessions in a day, each log is paired with the workout it was saved closest to. Energy in kJ and distances in miles are converted. The endpoint also accepts a simpler JSON shape (see `/openapi.yaml`) for your own Shortcut or script.

## Optional: connect a custom GPT Action

You can also connect a custom GPT to the workout API using the OpenAPI schema at `https://gym.tim0tei.fun/openapi.yaml`. Each person's GPT should use their own TimGym key. See OpenAI's [GPT Actions guide](https://developers.openai.com/api/docs/actions/introduction).

The API key grants access only to that person's workouts. Rotating it in TimGym invalidates the old key.

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not share account passwords or TimGym API keys.
