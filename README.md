# TimGym

A self-hosted personal workout and body-measurement tracker. Each account has private data.

## Deploy or update on CasaOS

From the folder containing `compose.yaml`, run:

```sh
docker compose up -d --build
```

Open `https://gym.tim0tei.fun` (or your server address). On first startup, Docker downloads the local AI model (about 3 GB); allow it time to finish.

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

## Log workouts with private local AI

Type a workout on the dashboard and choose **Interpret workout locally**. TimGym sends the note to Ollama on the private Docker network. Qwen 3.5 2B returns the workout structure; review it in the app and save only when it looks right.

The model runs on the CasaOS server CPU. It does not use ChatGPT or an OpenAI API, and workout notes are not sent to an external AI service. The model files persist in the `ollama_data` Docker volume.

The iPhone Shortcut in [SHORTCUT.md](SHORTCUT.md) remains an optional alternative. Each person using it needs their own private API key.

## Optional: connect a custom GPT Action

You can also connect a custom GPT to the workout API using the OpenAPI schema at `https://gym.tim0tei.fun/openapi.yaml`. Each person's GPT should use their own TimGym key. See OpenAI's [GPT Actions guide](https://developers.openai.com/api/docs/actions/introduction).

The API key grants access only to that person's workouts. Rotating it in TimGym invalidates the old key.

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume; Ollama model files are stored in `ollama_data`.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not share account passwords or TimGym API keys.
