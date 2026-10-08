# TimGym

A self-hosted personal workout and body-measurement tracker. Each account has private data.

## Deploy or update on CasaOS

From the folder containing `compose.yaml`, run:

```sh
docker compose up -d --build
```

Open `https://gym.tim0tei.fun` (or your server address).

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

## Log a workout from your iPhone without API billing

On the dashboard, type your workout in **Log with ChatGPT** and tap **Continue in ChatGPT Shortcut**. The app passes the text to the iPhone Shortcut named **TimGym Upload**. The Shortcut uses Apple's **Use Model → ChatGPT** to structure the workout, lets you review it, and posts it to TimGym with your personal TimGym key.

This does not use the OpenAI API or require an OpenAI API key. It requires an iPhone that supports Apple Intelligence's ChatGPT model action. Availability and usage limits depend on Apple and ChatGPT.

Build the Shortcut by following [SHORTCUT.md](SHORTCUT.md). Keep it private because it contains your TimGym key. Each person creates their own private Shortcut with their own key.

## Optional: connect a custom GPT Action

You can also connect a custom GPT to the workout API using the OpenAPI schema at `https://gym.tim0tei.fun/openapi.yaml`. Each person's GPT should use their own TimGym key. See OpenAI's [GPT Actions guide](https://developers.openai.com/api/docs/actions/introduction).

The API key grants access only to that person's workouts. Rotating it in TimGym invalidates the old key.

## Data and security

- SQLite is stored in the persistent `gym_data` Docker volume.
- Use a unique, long `SECRET_KEY` in your deployment environment if you manage environment variables. If omitted, TimGym generates a random session key and stores it in the data volume.
- The site uses secure, HTTP-only, same-site session cookies and CSRF protection.
- Back up the `gym_data` volume. Do not share account passwords or ChatGPT API keys.
