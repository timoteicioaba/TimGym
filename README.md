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

Type a workout on the dashboard and choose **Parse workout**. TimGym recognizes common forms such as `Squat 3x5 @ 80 kg`, `Bench press: 3 sets of 8 at 60 lb`, and separate exercises divided by a comma, semicolon, or “then”. It converts pounds to kilograms and supports multiple set prescriptions. Recognizable exercises still appear when sets or reps are missing, with fields to fill in directly in the preview. Review the result and save only when it looks right.

Parsing runs in the TimGym app itself. Workout notes are not sent to an AI service, no model is downloaded, and there is no extra API cost. Keep one exercise per clause for the clearest result.

The iPhone Shortcut in [SHORTCUT.md](SHORTCUT.md) remains an optional alternative. Each person using it needs their own private API key.

## Optional: connect a custom GPT Action

You can also connect a custom GPT to the workout API using the OpenAPI schema at `https://gym.tim0tei.fun/openapi.yaml`. Each person's GPT should use their own TimGym key. See OpenAI's [GPT Actions guide](https://developers.openai.com/api/docs/actions/introduction).

The API key grants access only to that person's workouts. Rotating it in TimGym invalidates the old key.

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not share account passwords or TimGym API keys.
