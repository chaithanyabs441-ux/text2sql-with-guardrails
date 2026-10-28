<p align="center">
  <img src="docs/assets/retro-safequery-ai-logo.png" alt="SafeQuery AI — verified read-only SQL" width="900">
</p>

<p align="center">
  <a href="https://github.com/chaithanyabs441-ux/text2sql-with-guardrails/actions/workflows/ci.yml"><img src="https://github.com/chaithanyabs441-ux/text2sql-with-guardrails/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/database-PostgreSQL%2016-336791" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/API-FastAPI-009688" alt="FastAPI">
</p>

<h3 align="center">Ask questions in plain English. Inspect the SQL. Get results through a read-only database account.</h3>

SafeQuery AI is a **text-to-SQL system built around the question "should you trust this query?"**
Instead of generating SQL and running it blindly, every request passes through layered defenses:
an AST-based SQL policy, a read-only PostgreSQL role with a statement timeout, result sanity checks,
and a calibrated confidence score that decides whether to **answer**, **warn**, or **abstain**.

> **Scope:** an experimental project for learning, demos, and evaluation, run against a synthetic
> sample database. It is **not** a production security boundary. See
> [Limitations](#limitations-and-safety-boundaries).

---

## Table of contents

- [Features](#features)
- [How a request flows](#how-a-request-flows)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Database schema](#database-schema)
- [Using the API](#using-the-api)
- [Query strategies](#query-strategies)
- [Defense layers](#defense-layers)
- [Evaluation and results](#evaluation-and-results)
- [Development](#development)
- [Project handoffs](#project-handoffs)
- [Limitations and safety boundaries](#limitations-and-safety-boundaries)
- [Repository map](#repository-map)
- [License](#license)

## Features

- **Fallout VideoGame/Series-themed web UI** with query history, example questions, result tables, and CSV download.
- **Database-backed login and role authorization** for employees, analysts, and admins, with protected admin controls and query history.
- **Full graphical schema view** with column types, primary/foreign-key markers, labeled relationship lines, and zoom controls.
- **Login-screen dataset selection and import** for CSV and Excel workbooks, while keeping the bundled `core` sample dataset as the default.
- **Schema-grounded SQL generation** using retrieved schema context, a business glossary, and examples.
- **AST-wide SQL policy** (via `sqlglot`): `SELECT`-only, single statement, table and function allowlists,
  sensitive-column blocking, join/subquery limits, tautology and cartesian-join detection, and forced `LIMIT` clamping.
- **Read-only execution sandbox**: separate PostgreSQL role, read-only transaction, 5 s `statement_timeout`.
- **Hallucination signals**: three-candidate result agreement, LLM back-translation similarity, self-reported
  confidence, schema coverage, and result sanity checks.
- **Calibrated answer / warn / abstain decisions** from a logistic model with thresholds chosen on out-of-fold predictions.
  Abstention hides the SQL and rows and offers alternative candidates instead.
- **Evaluation harness** for gold-query execution accuracy, confidence calibration, LLM red-teaming,
  and an offline SQL attack suite.
- **Provider-agnostic LLMs**: any OpenAI-compatible endpoint for generation and a separate judge model.

## How a request flows

```mermaid
flowchart LR
    U[Question] --> W[HTML frontend]
    W --> A[FastAPI]
    A --> G[SQL generation and schema context]
    G --> P[SQL policy checks]
    P --> X[Read-only execution sandbox]
    X --> D[(PostgreSQL sample database)]
    X --> S[Result sanity checks]
    G --> V[Agreement and back-translation signals]
    S --> C[Confidence and abstention]
    V --> C
    C --> A
    A --> W
```

1. The API retrieves relevant schema context (Sentence Transformers) and asks the generator for candidate SQL.
2. The policy engine parses and validates the SQL. **Rejected SQL never reaches the executor.**
3. The sandbox runs validated SQL in a read-only transaction with a timeout.
4. Three candidates at temperatures 0.0 / 0.3 / 0.7 are independently validated and executed to measure agreement.
5. A judge model back-translates the SQL into a question; similarity, agreement, sanity warnings, self-reported
   confidence, and schema coverage feed the calibrated model.
6. Thresholds choose `answer`, `warn`, or `abstain_with_alternatives`.

Every API operation that reads business data also requires a server-verified login. Role permissions and
per-user dataset grants are checked by the API; the browser only controls presentation.

## Quick start

**Prerequisites:** Docker Desktop and API keys for an OpenAI-compatible provider
(defaults to [Groq](https://groq.com)).

```bash
# 1. Create your environment file
cp .env.example .env          # PowerShell: Copy-Item .env.example .env

# 2. Set GENERATOR_API_KEY and JUDGE_API_KEY in .env  (this file is git-ignored)

# 3. Build and start PostgreSQL, the API, and the frontend
make
```

`make` prints the clickable frontend address (`http://localhost:8501`) after Compose starts, along with
the API docs and health URLs. If your terminal does not support clickable links, copy the printed URL.

| Service | URL |
| --- | --- |
| Frontend | <http://localhost:8501> |
| API docs (Swagger) | <http://localhost:8000/docs> |
| Health check | <http://localhost:8000/health> |
| PostgreSQL (host port) | `localhost:5433` |

Stop everything with `docker compose down`.

The first API start adds the authentication tables without dropping existing data. On a fresh database,
the same tables are also created by `docker/rbac_init.sql`.

### Set up the first administrator

There is no default administrator account. After the services are running, set a temporary username and
password in your shell and run the bootstrap command. Use a unique password with at least 12 characters;
the application stores only an Argon2 hash.

```bash
export BOOTSTRAP_ADMIN_USERNAME='your-admin-name'
read -s BOOTSTRAP_ADMIN_PASSWORD
export BOOTSTRAP_ADMIN_PASSWORD
make bootstrap-admin
unset BOOTSTRAP_ADMIN_USERNAME BOOTSTRAP_ADMIN_PASSWORD
```

PowerShell:

```powershell
$env:BOOTSTRAP_ADMIN_USERNAME = "your-admin-name"
$env:BOOTSTRAP_ADMIN_PASSWORD = Read-Host "Temporary administrator password"
docker compose exec -e BOOTSTRAP_ADMIN_USERNAME -e BOOTSTRAP_ADMIN_PASSWORD api python -m t2s.auth.bootstrap
Remove-Item Env:BOOTSTRAP_ADMIN_USERNAME, Env:BOOTSTRAP_ADMIN_PASSWORD
```

The API can also apply the additive migration explicitly with `make migrate`. Sign in at the frontend
using the bootstrapped credentials. The session cookie is HTTP-only, SameSite strict, and expires after
12 hours. Set `AUTH_COOKIE_SECURE=true` in `.env` when serving the app over HTTPS.

### Roles and permissions

| Role | Query responsibility | Other responsibilities |
| --- | --- | --- |
| Employee | Query the seven operational `core` tables: regions, categories, products, customers, orders, order items, and support tickets. | View the selected dataset schema and their own query history. Cannot query core workforce, finance, or returns tables. |
| Data Analyst | Query every registered `core` table, including workforce, payments, and returns. Imported datasets are available to all signed-in roles. | View their own history and query activity analytics. Cannot manage accounts, roles, grants, or audit logs. |
| Admin | Query every registered `core` table and every imported dataset table. | Create/manage accounts and roles, reset passwords, activate/deactivate users, grant/revoke `core` access, and view audit logs and query activity. |

The backend maps permissions centrally (`src/t2s/auth/permissions.py`) and checks them at each protected
endpoint. Changing a role in the browser or forging a request body does not grant access. Admins can
create accounts, change roles, reset passwords, deactivate accounts, and grant or revoke access to `core`.
Deactivation and password reset revoke existing sessions. Login attempts are rate limited. The API checks
every table in generated SQL against the caller's role and dataset access before execution, including SQL
generated for agreement checks and self-correction. Employees cannot query `payments`, `returns`, or
`employees`; those tables are available to analysts and admins. Admin user and audit data is managed in the
Admin Panel and is not exposed as a business SQL table.

Data access follows this path: the signed-in user's server-side role and dataset grants determine which
dataset/schema is available; the API builds schema context for that dataset; each generated SQL candidate
is checked against the role's table allowlist and dataset table list; only an authorized, policy-approved
query reaches the read-only database executor. A schema diagram is for discovery and does not itself grant
query access. For `core`, the employee diagram marks restricted tables, and the API rejects SQL that uses
them. Analysts get broader read access for business analysis. Admin privileges add account and access
management; they do not make generated SQL writable.

Authentication is implemented by this project with PostgreSQL-backed users and sessions; it does not use
Firebase. Passwords are stored as Argon2 hashes. The browser receives an HTTP-only, SameSite strict cookie
for a 12-hour session. The schema view shows all tables in a granted dataset, including employee-restricted
tables, and marks those restrictions; query access is checked separately by the API.

The local demo account workbook, if present, is `login ids/role-accounts.xlsx`. It is ignored by Git and may
contain plaintext passwords; keep it private. The database is authoritative for account creation, roles, and
passwords.

The bundled `core` dataset remains the default and is never overwritten by file imports. On the signed-out
login screen, anyone who can reach this local app can select an available dataset or import CSV/XLSX files.
Each import is written to a separate generated PostgreSQL schema, appears in the selector for all signed-in
users on this server, and is used as the schema context for query generation, SQL checks, execution, and the
schema diagram. CSV/XLSX imports have a 200 MB combined upload limit, up to ten files, 30 tables, 500,000 rows
per table, and 200 columns per table. Each CSV is one table; each worksheet in an XLSX file becomes a table.
Imports can infer primary keys from a unique, non-null entity key such as `port_id`, `route_id`, or from `id`;
when there is exactly one `*_id` column, it can serve as the entity key too. Foreign keys are inferred from
matching `*_id` columns (including prefixed references such as `origin_port_id` to `ports.port_id`) only
when types match and every non-null source value exists in the target key. The schema diagram displays the
database's resulting PK/FK metadata and draws labeled connectors for those foreign keys. If a dataset was
imported before key inference was improved, import its files again as a new dataset; existing imports are
not retroactively altered. Imported data is shared with every signed-in role; do not upload private or
confidential data. Imported datasets persist in PostgreSQL and currently have no delete UI.

Core access still uses per-user grants and strict employee table limits. Imported datasets are currently
available to all signed-in roles and allow each role to query every table in that dataset. Imports are
accepted before sign-in and are shared on this server, so authentication does not make imported data private
to its uploader. Dataset support does not include arbitrary live database connection strings, row-level
security, per-user column access, or per-dataset grants for imported datasets.

> **Heads up:** database init scripts only run when PostgreSQL first creates its data volume. If you have an
> older `pgdata` volume, the read-only role and sample schema won't be re-created. Run
> `docker compose down -v` to reset it (this deletes the local data).
>
> The first request downloads sentence-transformer weights, so it can be noticeably slower.

### Sample database

A synthetic business database (PostgreSQL schema `core`) with ten tables. Its full structure is in
[Database schema](#database-schema) below. A glossary in `src/t2s/schema/glossary.yaml` encodes business
definitions such as *revenue → `net_revenue`* and *customer → active, non-deleted customers*.

Try questions like:

- *Which five products have the highest unit prices?*
- *What is total revenue by region?*
- *How many pending orders are there per month?*

## Configuration

Settings are read from `.env` (see `.env.example`) via `src/t2s/config.py`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | local Postgres (`:5433`) | Admin connection, used for schema introspection |
| `DATABASE_URL_READONLY` | local Postgres (`:5433`) | **Read-only role** used to execute generated SQL |
| `GENERATOR_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible endpoint for SQL generation |
| `GENERATOR_API_KEY` | – | Generator key (**required**) |
| `GENERATOR_MODEL` | `llama-3.3-70b-versatile` | Generator model |
| `JUDGE_BASE_URL` | `https://api.groq.com/openai/v1` | Endpoint for the judge / back-translation model |
| `JUDGE_API_KEY` | – | Judge key (**required**) |
| `JUDGE_MODEL` | `qwen-2.5-coder-32b` | Judge model (a different model family adds diversity) |
| `AUTH_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS |
| `MAX_RETRIES` | `3` | Retry budget for LLM calls |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

Docker Compose overrides the two database URLs to point at the `db` container.

## Database schema

The sample database lives in the PostgreSQL schema `core` (see `docker/init.sql`). Data is synthetic and
generated at startup, roughly: 3 regions, 7 categories, 100 products, 50 employees, 10,000 customers,
50,000 orders, ~150,000 order items, plus payments, returns, and 5,000 support tickets (`docker/eval_seed.sql`).

### Entity-relationship diagram

```mermaid
erDiagram
    regions ||--o{ customers : "region_id"
    regions ||--o{ employees : "region_id"
    categories ||--o{ categories : "parent_id"
    categories ||--o{ products : "category_id"
    customers ||--o{ orders : "customer_id"
    customers ||--o{ support_tickets : "customer_id"
    employees ||--o{ orders : "employee_id"
    orders ||--o{ order_items : "order_id"
    orders ||--o{ payments : "order_id"
    products ||--o{ order_items : "product_id"
    order_items ||--o{ returns : "order_item_id"

    regions {
        int id PK
        varchar name
        varchar code UK
    }
    categories {
        int id PK
        varchar name
        int parent_id FK
    }
    products {
        int id PK
        varchar name
        int category_id FK
        numeric unit_price
        boolean is_active
    }
    customers {
        int id PK
        int region_id FK
        varchar name
        varchar segment
        timestamp created_at
        boolean is_deleted
    }
    employees {
        int id PK
        int region_id FK
        varchar first_name
        varchar last_name
        date hire_date
    }
    orders {
        int id PK
        int customer_id FK
        int employee_id FK
        varchar status
        timestamp order_date
        numeric gross_revenue
        numeric net_revenue
        text shipping_address
    }
    order_items {
        int id PK
        int order_id FK
        int product_id FK
        int quantity
        numeric discount_pct
        numeric line_total
    }
    payments {
        int id PK
        int order_id FK
        varchar payment_method
        numeric amount
        varchar status
        timestamp processed_at
    }
    returns {
        int id PK
        int order_item_id FK
        varchar reason
        numeric refund_amount
        date return_date
    }
    support_tickets {
        int id PK
        int customer_id FK
        varchar issue_type
        numeric resolution_time_hours
        timestamp created_at
    }
```

### Tables

| Table | Columns | Notes |
| --- | --- | --- |
| `regions` | `id` PK, `name`, `code` (unique) | Sales regions: North America, Europe, Asia Pacific |
| `categories` | `id` PK, `name`, `parent_id` → `categories.id` | Self-referencing hierarchy (e.g. Electronics → Laptops, Phones) |
| `products` | `id` PK, `name`, `category_id` → `categories.id`, `unit_price`, `is_active` | `is_active` marks products currently sold |
| `customers` | `id` PK, `region_id` → `regions.id`, `name`, `segment`, `created_at`, `is_deleted` | `segment` ∈ `enterprise`, `smb`, `consumer`. **Soft delete** via `is_deleted` |
| `employees` | `id` PK, `region_id` → `regions.id`, `first_name`, `last_name`, `hire_date` | `hire_date` is sometimes `NULL` |
| `orders` | `id` PK, `customer_id` → `customers.id`, `employee_id` → `employees.id`, `status`, `order_date`, `gross_revenue`, `net_revenue`, `shipping_address` | `status` ∈ `completed`, `pending`, `cancelled`, `returned`. `net_revenue` accounts for discounts and returns. `shipping_address` is **blocked by the SQL policy** |
| `order_items` | `id` PK, `order_id` → `orders.id`, `product_id` → `products.id`, `quantity`, `discount_pct`, `line_total` | `discount_pct` is often `NULL`/0 |
| `payments` | `id` PK, `order_id` → `orders.id`, `payment_method`, `amount`, `status`, `processed_at` | `status` ∈ `success`, `failed`, `refunded` |
| `returns` | `id` PK, `order_item_id` → `order_items.id`, `reason`, `refund_amount`, `return_date` | One row per returned line item |
| `support_tickets` | `id` PK, `customer_id` → `customers.id`, `issue_type`, `resolution_time_hours`, `created_at` | `resolution_time_hours` is often `NULL` (unresolved tickets) |

### Access model

- The application connects as **`t2s_readonly`** to run generated SQL. It has `USAGE` on schema `core` and
  `SELECT` only on its tables, with `default_transaction_read_only = on` and a 5 s `statement_timeout`.
- The admin role `t2s_user` is used for setup and schema introspection, never for generated queries.
- The SQL policy allowlists exactly the ten tables above, and rejects references to `orders.shipping_address`.
- To re-apply the read-only role to an existing database volume without recreating tables, run
  `docker/readonly_role.sql`; it is safe to rerun.

### Dataset selection and import

The login screen lists the existing datasets and defaults to **SafeQuery sample database (`core`)**. Choose
another dataset before signing in. Use **IMPORT CSV / EXCEL** to add one or more CSV files or an XLSX workbook;
each CSV is a table and each workbook sheet is a table. Imported datasets are stored separately, selected
immediately after import, and shared with every signed-in account on this server. Uploading does not erase
or replace `core`. Only CSV and XLSX are accepted; database dumps, arbitrary SQL, and live connection strings
are not accepted.

For automatic primary-key badges and relationship lines, use a unique, non-empty entity key such as
`port_id` in `ports`, `route_id` in `routes`, or the conventional `id`. Name references with the target
key suffix, such as `customer_id` for `customers.id` or `origin_port_id` for `ports.port_id`. The importer
adds a relationship only when types match and all non-null reference values resolve. Otherwise, the fields
remain ordinary columns and the diagram has no connector for that relationship.

### Schema view in the app

After signing in, choose **DATABASE SCHEMA** to see the full schema as an ER diagram. Every table box includes
its columns and database types, with PK/FK badges; labeled connectors show foreign-key relationships. Use
the zoom buttons to inspect the diagram. Employees see restricted tables marked in rust, while the API
continues to reject employee queries that reference those tables.

## Using the API

### Sign in from the command line

Create a session cookie before calling protected endpoints:

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username":"your-admin-name","password":"<your password>"}' \
  http://localhost:8000/auth/login
curl -b cookies.txt http://localhost:8000/auth/me
```

Do not store `cookies.txt` in the repository.

### `POST /v1/query`

```bash
curl -s http://localhost:8000/v1/query \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"question": "Which five products have the highest unit prices?",
       "strategy": "schema_glossary"}'
```

Response fields:

| Field | Description |
| --- | --- |
| `sql` | The validated, row-limited SQL that was executed |
| `explanation` | Model explanation of the query |
| `data` | Result rows |
| `warnings` | Sanity-check and policy warnings |
| `confidence_score` | Calibrated probability that the result is correct |
| `confidence_signals` | Individual signals (agreement, back-translation similarity, …) |
| `back_translated_question` | The judge's reconstruction of the question from the SQL |
| `decision` | `answer`, `warn`, or `abstain_with_alternatives` |
| `alternatives` | Other candidate queries (populated on abstention) |
| `calibration_ready` | Whether fitted calibration coefficients and thresholds were loaded |

Other endpoints: public `GET /datasets` lists available datasets and public `POST /datasets/import` accepts
multipart form data (`display_name` and one or more `files`); authenticated `GET /v1/schema?dataset_id=...`
returns the selected dataset schema and `GET /v1/history` returns recent queries for the signed-in user
(result rows are not retained). Authentication endpoints are `POST /auth/login`, `POST /auth/logout`,
`GET /auth/me`, and `GET /health`. The schema and query endpoints require a valid session. The bundled
`core` dataset also requires a user grant; imported datasets are shared with signed-in users on this server.
Admin routes under `/admin/` check their specific permissions server-side.

The frontend uses the same-origin proxy, so the browser does not need direct cross-origin API access.

If calibration artifacts are missing, the API falls back to conservative default thresholds and reports
`calibration_ready: false`.

## Query strategies

| Strategy | Context given to the model | Notes |
| --- | --- | --- |
| `schema_glossary` *(default)* | Schema + business glossary + examples | Best overall in the saved evaluation |
| `zero_shot` | Schema only | Baseline |
| `schema_glossary_selfcorrect` | Same as default | Retries after a database execution error |

All strategies share the same policy and read-only execution path. Self-correction only reacts to
execution errors; it cannot prove that a *successful* query is semantically correct.

## Defense layers

| Layer | What it does | Code |
| --- | --- | --- |
| **Schema grounding** | Limits the model's context to relevant tables and business definitions | `src/t2s/schema/` |
| **SQL policy engine** | Rejects non-`SELECT`, multi-statement, unapproved tables/functions, sensitive columns, tautologies, cartesian joins, excessive joins/subqueries; clamps `LIMIT`/`OFFSET` | `src/t2s/guardrails/` |
| **Read-only sandbox** | Dedicated Postgres role, read-only transaction, 5 s statement timeout | `src/t2s/sandbox/`, `docker/readonly_role.sql` |
| **Sanity checks** | Flags execution errors, empty results, mostly-negative values, huge row counts, high null ratios | `src/t2s/verification/sanity_checks.py` |
| **Agreement + back-translation** | Detects hallucination via candidate disagreement and meaning drift | `src/t2s/verification/` |
| **Calibration + abstention** | Turns signals into a probability and an answer / warn / abstain decision | `src/t2s/verification/calibration.py`, `abstention.py` |

Full threat model and trust boundaries: [docs/design.md](docs/design.md).

## Evaluation and results

Everything under `eval/` is reproducible and its outputs are saved to `eval/results/`.

| Script | Purpose |
| --- | --- |
| `eval/verify_gold.py` | Confirms the gold SQL executes |
| `eval/run_eval.py` | Execution accuracy vs. gold SQL on the golden set (78 questions, 55 with gold SQL) |
| `eval/run_calibration.py` | Fits the confidence model and abstention thresholds |
| `eval/run_redteam.py` | LLM-level adversarial prompts (development and held-out sets) |
| `eval/run_sql_attack_suite.py` | Offline attacks against the SQL policy, no LLM needed |

> ⚠️ Eval scripts that call a model provider consume quota, may incur cost, and may download embedding
> models. Check `.env` and script options before running large evaluations.

### Saved results (single run, observations, not guarantees)

**Execution accuracy** (golden set, 55 questions with gold SQL):

| Strategy | Execution accuracy | 95% CI | Valid SQL |
| --- | --- | --- | --- |
| `schema_glossary` | **72.7%** (40/55) | 59.0 – 83.9% | 94.5% |
| `schema_glossary_selfcorrect` | 72.7% (40/55) | 59.0 – 83.9% | 94.5% |
| `zero_shot` | 65.5% (36/55) | 51.4 – 77.8% | 96.4% |

Confidence intervals are wide, so differences between strategies are not statistically conclusive.

**Adversarial prompts** (LLM red-team):

| Set | Prompts | Blocked by guardrail | Refused by model | Executed harmless | Executed harmful |
| --- | --- | --- | --- | --- | --- |
| Development | 63 | 24 | 33 | 6 | **0** |
| Held-out | 25 | 6 | 17 | 2 | **0** |

On the development set, 1 of 20 benign prompts was wrongly blocked. A zero count of harmful executions
is an observation about these prompts, not proof that every attack is stopped. The 95% upper bound for
0/63 is about 5.7%.

**Offline SQL attack suite** (policy engine only, no LLM): 59 attack queries across catalog access, dangerous
functions, DDL/DML, multi-statement, tautologies, obfuscation, sensitive columns, and resource limits:
57 blocked, 2 mitigated (by clamping limits), **0 allowed**. 0 of 25 benign queries were blocked.

**Confidence calibration** (150 samples over 52 questions, grouped 5-fold CV × 3 repeats):

| Signal | AUROC |
| --- | --- |
| Calibrated combined model | **0.757** (95% CI 0.57 – 0.93) |
| Model's self-reported confidence | 0.461 (no better than chance) |

At the selected thresholds, queries marked `answer` were correct about 90% of the time at 53% coverage.
The calibrator's ECE is 0.150, so probabilities are useful for ranking but should not be read as exact
likelihoods. The takeaway from the ablation: an LLM's own stated confidence is not a reliable signal;
result agreement and back-translation are what carry the weight.

Charts: [`reliability.svg`](eval/results/calibration/reliability.svg),
[`selective_prediction.svg`](eval/results/calibration/selective_prediction.svg),
[`signal_ablation.svg`](eval/results/calibration/signal_ablation.svg).

## Development

```bash
# Install with dev dependencies (Python 3.11+)
python -m pip install -e ".[dev]"

# Run the tests (coverage is enabled by default)
pytest

# Lint
ruff check .
```

The test suite includes the original SQL policy tests plus auth/session invalidation, role escalation,
dataset grant, and nested SQL table-scope checks.

### Back up and restore

Create a custom-format backup of the running PostgreSQL database:

```bash
docker compose exec -T db pg_dump -Fc -U t2s_user -d t2s_db > safequery-backup.dump
```

Restore into a new, empty database so the running database is not overwritten:

```bash
docker compose exec db createdb -U t2s_user t2s_restore
docker compose exec -T db pg_restore -U t2s_user -d t2s_restore < safequery-backup.dump
```

The auth tables, users, grants, sessions, and audit events are in the same PostgreSQL database and are
included in this backup. Treat the backup as sensitive; it contains password hashes and security history.

Running the API and frontend outside Docker needs a PostgreSQL instance initialized with the scripts in
`docker/` (`init.sql`, `readonly_role.sql`, `eval_seed.sql`) and a populated `.env`:

```bash
uvicorn t2s.api.main:app --reload --port 8000
python frontend/app.py            # serves the UI and proxies /v1/query to the API
```

Test coverage includes the policy engine, a SQL attack suite, the LLM adapter, prompt building, schema
retrieval, agreement, back-translation, calibration, evaluation metrics, and the API. CI runs lint and tests
on every push and pull request.

## Project handoffs

- [Workflow quick guide](handoffs/workflow.md): startup, authentication, roles, imports, query flow, and security boundaries.
- [Phase 1 handoff](handoffs/handoff_phase_1.md): earlier Windows/local PostgreSQL bring-up and environment-specific notes.
- [Phase 2 handoff](handoffs/handoff_phase_2.md): current Docker Compose and Makefile workflow, database-backed roles, local account workbook, and full schema ER diagram.
- [Phase 3 handoff](handoffs/handoff_phase_3.md): adaptive schema diagram, imported dataset key inference, and the current role/data-access model and security boundaries.

## Limitations and safety boundaries

- **RBAC is an application-layer control.** The API performs deterministic permission checks and queries
  use the separate read-only PostgreSQL role, but this experimental project has not undergone a security
  audit or production hardening review.
- **Dataset support includes `core` plus uploaded CSV/XLSX data.** Dynamic connections to other databases,
  per-user grants for imported datasets, and row-level security are not implemented. Uploaded datasets are
  shared with signed-in users and allow all tables for each role; do not import confidential data.
- **The API uses its existing admin database URL for auth metadata and schema introspection.** Generated
  SQL still executes only through `t2s_readonly`; auth tables are excluded from the SQL allowlist. Protect
  database credentials and keep API/database ports on a trusted network.
- **Initial admin bootstrap must be run manually.** No admin is created automatically. Protect the
  bootstrap environment variables and remove them from the shell after use.
- **Cookie security depends on deployment transport.** Local HTTP uses `AUTH_COOKIE_SECURE=false`; use
  HTTPS and set it to `true` anywhere traffic leaves a trusted development machine.
- **Compose database credentials are the repository's existing local demo credentials.** They are not
  suitable for deployment on a shared or public network; replace the database credential setup before
  using a non-local deployment.

- **Confidence signals are not proof.** Agreement, back-translation similarity, model confidence, schema
  coverage, and calibration can all be wrong, and multiple candidates can share the same mistake.
- **Calibration is small-sample.** It is fitted on 150 samples from one dataset; thresholds may not transfer
  to other schemas, models, or question styles. Re-run it when generation or schema behavior changes.
- **Sanity checks are narrow.** They catch selected anomalies and do not validate business definitions.
- **SQL policy checks are not a complete security guarantee.** Policy gaps, configuration mistakes, database
  permissions, or dependency issues can change the protection provided.
- **Read-only does not mean private.** A permitted `SELECT` can reveal any data the role can read. There is no
  row-level policy, per-user column policy, or general PII redaction.
- **The data is synthetic** and not representative of real business data.
- **Prompts and schema context leave your machine** and are sent to the configured model provider. Don't use
  confidential data unless the provider is approved for it.
- **Availability and cost depend on external services**: rate limits, network failures, model changes, and
  embedding downloads all affect latency and success.
- **Not hardened for public deployment.** Login attempts are rate limited, but query requests have no
  per-user rate or cost quotas. Keep it on a trusted network.

## Repository map

| Path | Purpose |
| --- | --- |
| `frontend/` | Static HTML/CSS/JS frontend and a small same-origin API proxy |
| `src/t2s/api/` | FastAPI request handling |
| `src/t2s/auth/` | Database-backed users, sessions, role permissions, and dataset grants |
| `src/t2s/schema/introspector.py` | Reads table/column/key metadata for the selected dataset schema |
| `src/t2s/generation/` | Prompt construction and SQL generation |
| `src/t2s/schema/` | Schema introspection, retrieval, and the business glossary |
| `src/t2s/guardrails/` | SQL parsing and policy enforcement |
| `src/t2s/sandbox/` | Read-only database execution |
| `src/t2s/verification/` | Sanity checks, agreement, back-translation, calibration, abstention |
| `src/t2s/llm/` | OpenAI-compatible adapter, caching, and cost tracking |
| `docker/` | PostgreSQL schema, sample data, and read-only role setup |
| `eval/` | Golden set, red-team sets, evaluation scripts, and saved results |
| `tests/` | Unit and integration tests |
| `docs/design.md` | Threat model, trust boundaries, and design notes |
| `handoffs/` | Environment and project handoff notes |

## License

See [LICENSE](LICENSE).
