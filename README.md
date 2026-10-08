# TimGym

A self-hosted personal workout and body-measurement tracker. Each account has private data.

## Deploy or update on CasaOS

From the folder containing `compose.yaml`, run:

```sh
docker compose up -d --build
```

Open `https://gym.tim0tei.fun` (or your server address).

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

## Use ChatGPT to log workouts

Workout entry is available through the authenticated API, not the website.

1. Sign in to TimGym and choose **Create or rotate ChatGPT key**. Copy the key; it is shown only once. Rotating it invalidates the previous key.
2. Create a private ChatGPT GPT Action for that user and import `openapi.yaml` from this repository.
3. Set the Action authentication to **API Key**, using **Bearer** authentication, and paste that user's TimGym key.
4. In the Action instructions, tell ChatGPT to confirm the date, exercises, sets, reps, and weights with the user before calling `logWorkout`.

The API key grants access only to that person's workouts. Do not share it. The Action can log workouts and read recent workout history; body measurements remain in the signed-in website.

### Example workout request

```json
{
  "date": "2026-10-08",
  "exercises": [
    {
      "name": "Squat",
      "sets": [
        { "reps": 5, "weight_kg": 100, "rpe": 7 },
        { "reps": 5, "weight_kg": 100, "rpe": 8 }
      ]
    }
  ]
}
```

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not expose or share account passwords or ChatGPT API keys.
