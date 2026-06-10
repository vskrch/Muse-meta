# Muse Meta

Muse Meta is an experimental FastAPI proxy that exposes a limited
OpenAI-compatible chat completion API over Meta AI web session credentials.

## Legal And Safety Notice

This project is for educational, interoperability, and authorized testing only.
Use it only with accounts, sessions, systems, and data you are authorized to use.
You are responsible for complying with applicable laws, platform terms,
acceptable-use policies, rate limits, and privacy requirements.

Do not use this project for spam, scraping, credential misuse, evading access
controls, bypassing service restrictions, abusive automation, harassment,
fraud, or any activity that harms users, services, or infrastructure. Do not
deploy it as a public unauthenticated proxy. This software is provided as-is,
without warranty, and the maintainers are not responsible for misuse or for
third-party service changes.

This client relies on undocumented web behavior and may stop working without
notice. Treat all Meta session cookies, access tokens, browser profiles, and
captured request logs as secrets. If any of those artifacts were ever committed
or pushed, rotate the affected sessions immediately.

## What It Provides

- OpenAI-style endpoints: `GET /v1/models` and `POST /v1/chat/completions`.
- Streaming responses using server-sent events.
- Inbound bearer-token authentication for all `/v1/*` routes.
- Production config validation that refuses unsafe production settings.
- Host allowlisting, strict opt-in CORS, security headers, request-size limits,
  and per-process rate limiting.
- Health endpoints: `/health` for liveness and `/ready` for configuration
  readiness.
- Upstream resilience: bounded retries with jitter, persisted state under
  `.state/`, doc-id fallback, and challenge retry handling where possible.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
API_KEY=change-this-long-random-token
META_AI_DATR=your_datr_cookie
META_AI_ECTO_1_SESS=your_ecto_1_sess_cookie
```

Run locally:

```bash
python3 -m muse_meta
```

Test the API:

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer change-this-long-random-token"
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer change-this-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Production Configuration

Set explicit production values. The app refuses to start in `production` if
debug is enabled, API auth is disabled, no API key exists, or hosts are
wildcarded.

```dotenv
ENVIRONMENT=production
DEBUG=false
DOCS_ENABLED=false

REQUIRE_API_KEY=true
API_KEY=replace-with-a-long-random-secret
ALLOWED_HOSTS=api.example.com
CORS_ALLOW_ORIGINS=https://app.example.com
CORS_ALLOW_CREDENTIALS=false

MAX_REQUEST_BYTES=1048576
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
REQUEST_TIMEOUT=60.0

STATE_DIR=/var/lib/muse-meta
META_AI_DATR=...
META_AI_ECTO_1_SESS=...
```

Deployment notes:

- Terminate TLS at a trusted reverse proxy or platform load balancer.
- Do not enable broad proxy-header trust for internet traffic.
- Keep `/v1/*` behind bearer-token auth and rotate `API_KEY` on exposure.
- Keep `.env`, `.state/`, browser profiles, and request captures out of git,
  images, logs, and support bundles.
- Run multiple replicas at the platform level if you need higher availability;
  the included rate limiter is per process.
- Use `/health` for liveness and `/ready` for configuration readiness.

## Docker

```bash
docker build -t muse-meta .
docker run --rm -p 8000:8000 --env-file .env muse-meta
```

The image installs only runtime API dependencies and runs as a non-root user.
Local browser extraction tools are not installed into the production image.

## Local Cookie Tools

Install tool dependencies only when you need local credential extraction:

```bash
pip install -e ".[tools]"
python3 -m playwright install chromium
python3 tools/extract_cookies.py
```

Generated cookies and browser profiles default to `.state/`. These files are
sensitive and ignored by git.

## Project Structure

```text
.
├── Dockerfile
├── README.md
├── pyproject.toml
├── src/muse_meta/
│   ├── __main__.py
│   ├── config.py
│   ├── main.py
│   ├── middleware.py
│   ├── security.py
│   ├── models/
│   ├── routers/
│   └── services/
├── tests/
└── tools/
```

## Development

```bash
ruff check .
ruff format .
mypy src
pytest
```

Using the bundled virtualenv in this workspace:

```bash
./venv/bin/python -m ruff check .
./venv/bin/python -m ruff format .
./venv/bin/python -m mypy src
./venv/bin/python -m pytest -q
```

## License

Apache-2.0. See [LICENSE](LICENSE).
