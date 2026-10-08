# TimGym

A small self-hosted personal gym log for workouts and body measurements.

## Run with Docker Compose

1. Copy this repository to your CasaOS server.
2. From the project folder, run `docker compose up -d --build`.
3. Open `http://<server-ip>:8000`.

SQLite data is stored in the `gym_data` Docker volume and survives container restarts.

## First version

- Log workouts by date, exercise, sets, reps, and weight.
- Record body weight, body-fat percentage, and optional waist, chest, and hip measurements.
- Review recent entries and a body-weight trend chart.

This initial version has no login. Keep it on a trusted network; add authentication before exposing it publicly. Apple Health sync and ChatGPT/MCP integration are planned for later.

## Configuration

Set `PORT` to change the host port (default `8000`). The container listens on port `8000`.
