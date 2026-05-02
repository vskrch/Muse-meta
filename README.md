# Muse Meta

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

A reverse proxy that exposes **Meta AI Muse Spark** chat completions through an **OpenAI-compatible API**. Built with [FastAPI](https://fastapi.tiangolo.com/) and modern Python practices.

## Features

- **OpenAI API compatibility** — Drop-in replacement for OpenAI chat completions.
- **Streaming support** — Real-time server-sent events for responsive UIs.
- **Fast & async** — Built on `asyncio` and `httpx` for high concurrency.
- **PEP 20 aligned** — Clean, readable, explicit code with `ruff` linting.
- **Pydantic v2** — Type-safe request/response models.

## Quick Start

### Prerequisites

- Python 3.11 or newer
- A Meta AI Muse Spark API key

### Installation

```bash
# Clone the repository
git clone https://github.com/vskrch/Muse-meta.git
cd Muse-meta

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

### Configuration

Copy the example environment file and fill in your API key:

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
MUSE_API_KEY=your_meta_ai_api_key_here
```

### Run the Server

```bash
# Using uvicorn directly
uvicorn muse_meta.main:app --reload --host 0.0.0.0 --port 8000

# Or with the run script
python3 -m muse_meta.main
```

The API will be available at `http://localhost:8000`.

Visit `http://localhost:8000/docs` for interactive API documentation.

## Usage

### Non-streaming Completion

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "Hello, world!"}
    ]
  }'
```

### Streaming Completion

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "Tell me a joke."}
    ],
    "stream": true
  }'
```

## Project Structure

```
Muse-meta/
├── src/
│   └── muse_meta/
│       ├── __init__.py
│       ├── main.py              # FastAPI app & lifespan
│       ├── config.py            # Pydantic settings
│       ├── models/              # OpenAI-compatible Pydantic models
│       │   ├── __init__.py
│       │   └── chat.py
│       ├── routers/             # API route handlers
│       │   ├── __init__.py
│       │   └── chat.py
│       ├── services/            # External API client
│       │   ├── __init__.py
│       │   └── muse_client.py
│       └── utils/               # Shared helpers
│           └── __init__.py
├── tests/                       # Pytest test suite
├── .env.example
├── .gitignore
├── LICENSE
├── pyproject.toml               # Project config, Ruff, pytest, mypy
└── README.md
```

## Development

### Linting & Formatting

This project uses **[Ruff](https://docs.astral.sh/ruff/)** for blazing-fast linting and formatting aligned with PEP 20 principles.

```bash
# Check all files
ruff check .

# Auto-fix issues
ruff check . --fix

# Format all files
ruff format .
```

### Type Checking

```bash
mypy src
```

### Running Tests

```bash
pytest
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MUSE_BASE_URL` | `https://www.meta.ai/api` | Base URL for Meta AI Muse Spark |
| `MUSE_API_KEY` | *(empty)* | API key for Muse Spark authentication |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8000` | Server bind port |
| `DEBUG` | `false` | Enable debug mode |
| `REQUEST_TIMEOUT` | `60.0` | Upstream request timeout (seconds) |

## License

This project is licensed under the [Apache License 2.0](LICENSE).
