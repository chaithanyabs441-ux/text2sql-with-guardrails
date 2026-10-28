# SafeQuery AI — Phase 2 handoff

Date: 27 October 2026  
Workspace: `/home/death-kid/IDE/text2sql-with-guardrails`

This handoff records the local Docker bring-up, role-based login work, schema-view changes, and operating notes for the current workspace. Phase 1 describes an earlier Windows/PostgreSQL setup; its machine-specific paths and process commands do not describe this Linux/Docker workspace. See [handoff_phase_1.md](handoff_phase_1.md) for that earlier setup history.

## Current state

At the time of this handoff, `docker compose ps` showed PostgreSQL, the API, and the frontend running; PostgreSQL and the API reported healthy. The HTTP health check at `http://localhost:8000/health` and the frontend at `http://localhost:8501` both returned HTTP 200.

The current project uses Docker Compose and `make`. `make` builds and starts the complete stack in the background. The database remains in the Compose `pgdata` volume. Do not use `docker compose down -v` unless you intend to delete that database volume.

## What was added or changed

### Makefile and local startup

- The default `make` target is `up`, which checks Docker, Compose, and the local `.env`, then runs `docker compose up --build -d`.
- `make setup` creates `.env` from `.env.example` if it does not already exist.
- `make status`, `make logs`, `make follow`, `make down`, and `make restart` provide common Compose controls.
- `make migrate` applies the additive authentication schema. `make bootstrap-admin` creates the first administrator from temporary environment variables.
- `.env` is ignored by Git. Keep provider keys there; do not paste them into handoffs or commit them.

Typical start:

```bash
make setup                 # only if .env does not exist
# Edit .env and set GENERATOR_API_KEY and JUDGE_API_KEY
make
make status
```

Open the app at <http://localhost:8501>, API docs at <http://localhost:8000/docs>, and API health at <http://localhost:8000/health>. `make down` stops the stack while preserving the database volume.

### Authentication and roles

Authentication is implemented in the FastAPI application and PostgreSQL. **It does not use Firebase.** The database stores users, Argon2 password hashes, hashed session tokens, dataset grants, login attempts, and audit events. The browser receives an HTTP-only, SameSite strict session cookie with a 12-hour lifetime; `AUTH_COOKIE_SECURE=true` should be used over HTTPS.

Roles are enforced by the API, not by hiding controls in the browser:

| Role | Query access | Other access |
| --- | --- | --- |
| Employee | Seven operational `core` tables; all tables in an imported dataset | Full schema view, own query history |
| Analyst | All ten registered `core` tables; all tables in an imported dataset | Own history and query activity analytics |
| Admin | All ten registered `core` tables; all tables in an imported dataset | User and role management, account activation/deactivation, password reset, `core` grants, audit logs, and the supported dataset controls |

Every final SQL candidate is parsed and checked against both the dataset grant and the role’s physical-table allowlist, including candidates used for agreement and self-correction. Employees cannot query `employees`, `payments`, or `returns`. The existing SQL policy and read-only PostgreSQL executor still apply to every role.

For `core`, role separation is based on the tables a query references and on protected API operations, not on a special phrase in the question. Analysts and admins both have access to all ten business tables; admin-only capabilities are account, grant, and audit management. Imported datasets intentionally grant every signed-in role access to every table because the login page is public and imports are shared. A useful core cross-role check is to request a payments/returns result as an employee (expect a 403), repeat it as an analyst (query allowed), and attempt user management as an analyst (expect a 403).

The first administrator is created once per database using shell environment variables, not a hard-coded default account:

```bash
export BOOTSTRAP_ADMIN_USERNAME='your-admin-name'
read -s BOOTSTRAP_ADMIN_PASSWORD
export BOOTSTRAP_ADMIN_PASSWORD
make bootstrap-admin
unset BOOTSTRAP_ADMIN_USERNAME BOOTSTRAP_ADMIN_PASSWORD
```

The password must be at least 12 characters. Once signed in as admin, create employee, analyst, and additional admin accounts in the Admin Panel. User creation, role changes, resets, deactivation, and dataset grants are stored in the database and audited.

### Local role-account workbook

`login ids/role-accounts.xlsx` is a local convenience sheet for the role-specific demo usernames and passwords. It is explicitly ignored by Git. The workbook contains plaintext passwords: keep it private and do not upload it, commit it, or treat it as the source of account state. The PostgreSQL authentication tables are authoritative. The folder name is `login ids`; the earlier `test` folder was removed.

### Database schema view

After signing in, use **DATABASE SCHEMA** beside **ASK DATA**. The view displays the selected dataset as an ER diagram: table boxes contain column names and types, PK/FK badges mark keys, and labeled lines connect foreign-key relationships. Zoom controls are available. The diagram expands across the content width and keeps the established Fallout palette and styling. For `core`, employee-only restrictions are marked in rust and still enforced by API query authorization.

### Login-page dataset selection and import

Before signing in, the login screen lists datasets and lets anyone with access to the app switch from the
default `core` dataset or upload CSV/XLSX. Each CSV is one table and each XLSX sheet becomes a table. Uploads
are stored in separate generated PostgreSQL schemas, so they do not overwrite core; they are immediately
selected and shared with every signed-in account on this server. CSV/XLSX uploads are limited to 200 MB total,
ten files, 30 tables, 500,000 rows per table, and 200 columns per table. For automatic diagram relationships,
provide a unique non-empty `id` column and matching reference names such as `customer_id` pointing to
`customers.id`; the importer adds constraints only when types and values validate. Do not import private or
confidential data. Imported datasets currently have no delete UI and role-specific table restrictions do not
apply to them.

The schema endpoint returns the selected schema only after authentication. `core` also requires a per-user dataset grant; imported datasets are intentionally shared with all signed-in roles. Showing a table in the diagram does not grant any additional permission beyond those dataset rules.

### Common access errors

- `401 Authentication required`: sign in again; the session cookie may be absent, expired, or revoked.
- `403 You do not have access to this dataset`: an admin needs to grant the user access to `core` in the Admin Panel.
- `403 This question needs a table that is not available to your role`: the question references a table beyond that account’s role. Use an analyst/admin account when the task requires those tables.

## Key files

| Path | Purpose |
| --- | --- |
| `Makefile` | Compose startup, status/log controls, migration, and admin bootstrap |
| `docker-compose.yml` | PostgreSQL, API, and frontend services |
| `docker/rbac_init.sql` | Additive authentication tables and initial `core` dataset registration |
| `src/t2s/auth/` | Password hashing, sessions, grants, permissions, migration, and bootstrap |
| `src/t2s/auth/permissions.py` | Role permission and query-table matrices |
| `src/t2s/api/main.py` | Auth endpoints, admin operations, schema/query/history authorization |
| `frontend/index.html` | Login, role UI, ER diagram, admin panel, and query view |
| `frontend/styles.css` | Fallout-themed visual styling, including the schema diagram |
| `login ids/role-accounts.xlsx` | Local-only role-account convenience workbook; ignored by Git |
| `README.md` | User-facing setup, role, schema, API, and safety documentation |
| `docs/design.md` | Threat model and trust boundaries |

## Verification and limits

The inline JavaScript passed `node --check`; Python sources passed `compileall`; `git diff --check` passed; Docker rebuilt both app images and its API import check passed. After rebuilding, `/health` returned `{"status":"ok"}`, `/datasets` returned the default `core` dataset, and the frontend served the dataset picker and import form. The PostgreSQL, API, and frontend containers were running; PostgreSQL and API were healthy. No sample file was uploaded, and the full pytest suite was not run.

This remains a local demo against synthetic data, not a production-ready identity or data-security service. Authentication, role enforcement, and dataset grants are application-layer controls and have not had an independent security audit. Login attempts are rate-limited; query calls do not have per-user rate or cost quotas. Imported datasets are shared with every signed-in role; row-level and per-user column policies are not implemented. Model prompts and selected schema context go to the configured external provider.

The health endpoint only shows that the API process is responding; it does not confirm provider availability or that a query will succeed.
