# PathMind Atlas

PathMind Atlas is a Python 3.10+ research/workflow application for de-identified surgical pathology case tracking, metric-based whole-slide image QC, synoptic completeness and consistency checks, supervisor review, and an in-process HMAC-SHA256 audit chain. It provides a CLI, FastAPI REST API, and a browser operations console served by FastAPI.

**Not a validated medical device or clinical decision system.** QC thresholds and stage/margin checks are simplified heuristics, not a substitute for current CAP/AJCC protocol rules or a qualified pathologist. Do not enter identifiable patient data.

## Run locally

```bash
git clone https://github.com/abusuraihsakhri/pathmind-atlas.git
cd pathmind-atlas
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export PATHMIND_HMAC_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python cli.py init
python cli.py serve
```

Open **http://127.0.0.1:8000/** for the web console, or **http://127.0.0.1:8000/docs** for OpenAPI documentation. Windows users can activate with `.venv\Scripts\activate` and set environment variables with their shell's syntax. Localhost is the default bind address.

The console calls the *running backend*. Register an anonymized case, run QC metrics, validate a synoptic report, and then request supervisor review. The review cannot approve cases with no slides, no synoptic reports, rejected/warning QC, or critical validation flags. API errors are shown, not replaced with sample output. For complete protocol input fields, use `/docs`.

Useful CLI commands:

```bash
python cli.py --help
python cli.py create-case --help
python cli.py qc-slide --help
python cli.py validate-synoptic --help
python cli.py review-case --help
python cli.py audit-verify
```

The command-line entry point `pathmind` is also installed. The CLI uses Click, so subcommands appear with hyphens.

## API

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Same-origin operations console |
| GET | `/health` | Health response |
| GET | `/metrics` | Prometheus metrics |
| POST / GET | `/api/cases` | Register / list de-identified cases |
| GET | `/api/cases/{case_id}` | Case detail |
| POST | `/api/slides/qc` | Score measured QC inputs |
| POST | `/api/synoptic/validate` | Check reported synoptic fields |
| POST | `/api/cases/{case_id}/review` | Supervisor summary |
| GET | `/api/audit` | In-memory audit events |
| GET | `/api/audit/verify` | Verify HMAC chain |
| POST | `/api/chat` | Operations assistant |

## Configuration and privacy

| Variable | Meaning |
|---|---|
| `PATHMIND_HMAC_SECRET` | **Required**, at least 32 characters; used to sign audit events |
| `PATHMIND_API_KEY` | Optional on loopback, **required for nonlocal API access**; at least 32 characters, supplied through `X-Pathmind-API-Key` |
| `PATHMIND_DATABASE_URL` | SQLAlchemy URL (default `sqlite:///pathmind_atlas.db`) |
| `PATHMIND_CORS_ORIGINS` | Comma-separated allowed origins (default localhost:8000 and :3000) |
| `PATHMIND_LLM_PROVIDER` | `MOCK` (default), `OLLAMA`, `CLAUDE`, `OPENAI` |
| `OLLAMA_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Provider-specific settings |

The PHI regex guard detects **some** identifiers; it cannot guarantee de-identification or regulatory compliance. Case data are persisted in the configured database. The HMAC audit chain and its events are **process-local memory only**, are lost on restart, and are not a durable/legal audit record. If an external language-model provider is configured, de-identified prompts leave the local service; use `MOCK` for offline deterministic responses.

Local API calls without a configured API key work only from the loopback interface (and the test client). Requests from other hosts are refused without `PATHMIND_API_KEY`. When an API key is configured, every route except `/` and `/health` requires the header. The console has a password field for this key, which it does not persist. For any remote use, provide TLS and a suitably controlled deployment; an API key alone is not comprehensive security.

## Docker

Provide fresh secrets rather than committing them:

```bash
export PATHMIND_HMAC_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PATHMIND_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up --build
```

The Compose port is bound to `127.0.0.1:8000` by default. The browser console needs the API key from the environment to call protected routes. The Docker filesystem is ephemeral unless you mount persistent storage for SQLite; do not rely on container restarts to preserve cases.

## Tests

```bash
python -m pip install -e ".[test]"
python -m pip check
python -m compileall -q agents cli.py enrichment.py simulator.py tests
ruff check --select E4,E7,E9 .
python -m pytest -q
```

GitHub Actions tests Python 3.10, 3.11 and 3.12 and checks the browser JavaScript syntax. This is a backend application with a database, signing key, and optional LLM connections. **GitHub Pages cannot host its actual workflows**, and conversion to browser Python would not preserve the server-side persistence and security model.

## Technology and compatibility

Python, FastAPI, SQLAlchemy/SQLite, Pydantic, Click, Rich, and plain HTML/CSS/JavaScript. The console uses modern browser fetch and form APIs; it has no CDN runtime dependencies. REST access is also available without the console.

## License

The repository does not currently include a LICENSE file. Confirm licensing terms with the repository owner before reuse or redistribution.
