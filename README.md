# Pathmind Atlas

> **Domain:** Clinical Decision Support & Biomedical Computing
> **Reference Guidelines & Standards:** `Standard Clinical Formulations & ISO/IEC Quality Frameworks`

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688.svg?logo=fastapi&logoColor=white)
![Audit Trail](https://img.shields.io/badge/Audit-HMAC--SHA256_Tamper--Evident-brightgreen.svg)
![Zero-PHI Guard](https://img.shields.io/badge/Guard-Zero--PHI_Outbound-blue.svg)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)

</div>

---

## What It Does

**Pathmind Atlas** is an advanced analytical and computational platform implementing Whole-Slide Imaging Laplacian blur QC worker, CAP cancer synoptic protocol margin checker (0.0mm panic alert), zero-PHI outbound guard, HMAC audit trail, FastAPI REST API, Web Dashboard & CLI. (39/39 tests passing).

---

## Key Capabilities & Algorithmic Modules

- **Deterministic Calculation Engine**: Strict compliance with standard reference formulations and thresholds.
- **Risk & Urgency Classification**: Multi-tier categorization with automated clinical/operational action recommendations.
- **Validation & Guardrails**: Rigorous input bounds checking and anomaly detection.
- **Cryptographic Audit Trail**: Tamper-evident HMAC-SHA256 chained audit logs.
- **Zero-PHI Outbound Guard**: Active regex inspection blocking SSNs, MRNs, phone numbers, and patient identifiers.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/abusuraihsakhri/pathmind-atlas.git
cd pathmind-atlas

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Set required environment variable for HMAC audit security
export PATHMIND_HMAC_SECRET="your-secure-32-character-minimum-secret-key"
```

---

## CLI Quickstart & Usage

### 1. Guided Interactive Mode
```bash
python cli.py
```

### 2. Direct Parameterized Evaluation
```bash
python cli.py --accession <value> --organ <value> --specimen <value> --history <value>
```

### 3. Initialize Database
```bash
python cli.py init
```

### 4. Create a Case
```bash
python cli.py create_case --accession S26-1002 --organ BREAST --specimen "Total Mastectomy" --history "Invasive ductal carcinoma"
```

### 5. Run Slide QC
```bash
python cli.py qc_slide --case-id CASE-XXX --blur-var 1200.0 --tissue-fold 0.5 --air-bubble 0.0
```

### 6. Validate Synoptic Report
```bash
python cli.py validate_synoptic --case-id CASE-XXX --protocol BREAST_INVASIVE --histology "Invasive Ductal Carcinoma" --size 2.4 --deep-margin 3.5 --radial-margin 1.2
```

### 7. Review Case (Supervisor Orchestration)
```bash
python cli.py review_case --case-id CASE-XXX
```

### 8. Verify Audit Integrity
```bash
python cli.py audit_verify
```

### 9. Launch API Server
```bash
python cli.py serve --host 127.0.0.1 --port 8000
```

---

## REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | System health check |
| GET | `/metrics` | Prometheus operational metrics |
| POST | `/api/cases` | Create new pathology case |
| GET | `/api/cases` | List all cases |
| GET | `/api/cases/{case_id}` | Get case details |
| POST | `/api/slides/qc` | Submit slide for QC evaluation |
| POST | `/api/synoptic/validate` | Validate synoptic cancer report |
| POST | `/api/cases/{case_id}/review` | Orchestrate full case review |
| GET | `/api/audit` | Get cryptographic audit trail |
| GET | `/api/audit/verify` | Verify audit chain integrity |
| POST | `/api/chat` | Operations chat assistant |

---

## Security & Enterprise Architecture

* **Zero-PHI Outbound Interceptor:** Active regex inspection blocking SSNs, MRNs, phone numbers, and patient identifiers.
* **Tamper-Evident HMAC-SHA256 Audit Trail:** Chained, cryptographically signed logs for every evaluation and state transition.
* **Secure Secret Management:** HMAC secret must be provided via `PATHMIND_HMAC_SECRET` environment variable (minimum 32 characters).
* **CORS Protection:** Configurable allowed origins via `PATHMIND_CORS_ORIGINS` environment variable.
* **Input Validation:** All API endpoints enforce strict bounds checking on clinical measurements.
* **Air-Gapped LLM Reasoning Adapter:** Agnostic integration for local Ollama instances, Claude 3.5 Sonnet, GPT-4o, and deterministic test mocks.

---

## Testing & Verification

Run the full automated test suite (39 tests):

```bash
# Set test HMAC secret
export PATHMIND_HMAC_SECRET="test-secret-key-for-pytest-min-32-chars-long"

# Run all tests
pytest -v

# Run specific test modules
pytest tests/test_pathmind_atlas.py -v
pytest tests/test_security.py -v
pytest tests/test_enrichment.py -v
```

Execute high-throughput batch simulation benchmarks:

```bash
python simulator.py 1000
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PATHMIND_HMAC_SECRET` | Yes | HMAC signing secret (min 32 chars) |
| `PATHMIND_DATABASE_URL` | No | Database URL (default: `sqlite:///pathmind_atlas.db`) |
| `PATHMIND_LLM_PROVIDER` | No | LLM provider: `MOCK`, `OLLAMA`, `CLAUDE`, `OPENAI` |
| `PATHMIND_CORS_ORIGINS` | No | Comma-separated allowed CORS origins |
| `OLLAMA_BASE_URL` | No | Ollama server URL |
| `ANTHROPIC_API_KEY` | No | Anthropic Claude API key |
| `OPENAI_API_KEY` | No | OpenAI API key |

---

## Container Deployment

```bash
# Set your production HMAC secret
export PATHMIND_HMAC_SECRET="your-production-secret-min-32-chars"

# Build and run with Docker Compose
docker-compose up --build

# Or use Docker directly
docker build -t pathmind-atlas .
docker run -p 8000:8000 -e PATHMIND_HMAC_SECRET="your-secret" pathmind-atlas
```

---

## Project Structure

```
pathmind-atlas/
├── agents/              # Core agent modules
│   ├── api.py           # FastAPI REST endpoints
│   ├── base.py          # Security, PHI guard, HMAC audit
│   ├── llm_factory.py   # LLM provider abstraction
│   ├── models.py        # SQLAlchemy database models
│   ├── qc_worker.py     # Slide QC agent
│   ├── supervisor.py    # Supervisor orchestrator
│   └── synoptic_worker.py  # CAP synoptic validator
├── tests/               # Test suite
│   ├── conftest.py      # Shared test configuration
│   ├── test_pathmind_atlas.py  # Core functionality tests
│   ├── test_security.py  # Security & validation tests
│   └── test_enrichment.py # Enrichment module tests
├── web/                 # Web dashboard
├── cli.py               # CLI entry point
├── simulator.py         # High-throughput simulator
├── enrichment.py        # Analytics enrichment modules
├── pyproject.toml       # Project configuration
├── Dockerfile           # Container build
└── docker-compose.yml   # Compose orchestration
```
