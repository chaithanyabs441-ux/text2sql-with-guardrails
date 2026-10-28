# SafeQuery AI — Project workflow

This is the quick guide to how the project is built and how a request moves through it. For setup, full
schema details, and security limits, see [README.md](../README.md). For phase-by-phase history, see the
other files in this folder.

## 1. Start the app

1. Copy `.env.example` to `.env` if needed, then add `GENERATOR_API_KEY` and `JUDGE_API_KEY`.
2. Run `make` in the project root. It builds and starts PostgreSQL, the API, and the web frontend.
3. Open the printed link, usually <http://localhost:8501>. The API docs are at
   <http://localhost:8000/docs>.
4. Create the first admin from a terminal with `make bootstrap-admin`; the app has no built-in default
   login. For example, in Bash:

   ```bash
   export BOOTSTRAP_ADMIN_USERNAME='your-admin-name'
   read -s BOOTSTRAP_ADMIN_PASSWORD
   export BOOTSTRAP_ADMIN_PASSWORD
   make bootstrap-admin
   unset BOOTSTRAP_ADMIN_USERNAME BOOTSTRAP_ADMIN_PASSWORD
   ```

   Use a password of at least 12 characters. The admin can then create employee, analyst, and additional
   admin accounts.

Use `make status` to check services, `make logs` to inspect recent logs, and `make down` to stop the
services while preserving the database volume.

## 2. Sign-in and authentication

Authentication is implemented in the project with FastAPI and PostgreSQL; it is not Firebase.

1. A user signs in with a username and password.
2. Passwords are hashed with Argon2 before storage. The API rate-limits failed login attempts.
3. On successful login, the server creates a random session token and stores its SHA-256 digest in the
   auth tables. The browser receives the token in an HTTP-only, SameSite strict cookie (12-hour lifetime).
4. Protected API calls validate that session against PostgreSQL and load the user's current role. Logout,
   expiration, password reset, and account deactivation invalidate sessions.
5. Every protected action checks permissions on the server. Hiding a button in the UI is not the security
   check.

## 3. Roles and responsibility

| Role | What the role can query | Other capabilities |
| --- | --- | --- |
| Employee | Seven operational `core` tables: regions, categories, products, customers, orders, order items, and support tickets. | View the schema and their own query history. The API blocks core workforce, payment, and return tables. |
| Analyst | All ten `core` tables, including workforce, payments, and returns. | View their own history and their query activity analytics. Cannot manage accounts, roles, grants, or audit logs. |
| Admin | All ten `core` tables. | Manage users and roles, reset passwords, activate/deactivate accounts, grant/revoke `core` access, and view audit logs and all query activity. |

Core dataset access is granted per user. Admins bypass the `core` grant check. Imported datasets are a
special case: imports are available to every signed-in role, and every role can query every table in them.
The import form works before sign-in, so uploads are shared on this server and are not private to the
uploader. Do not import confidential data.

## 4. Dataset import and schema diagram

The login screen starts with the bundled `core` dataset selected. A user can choose an available dataset or
import CSV/Excel files before signing in. Imports create a separate PostgreSQL schema and do not overwrite
`core`. Each CSV file becomes one table; each Excel worksheet becomes one table. Select all related CSV files
together if the dataset has multiple tables. Imports allow up to 200 MB total, 10 uploaded files, 30 tables,
500,000 rows per table, and 200 columns per table. This feature imports CSV/Excel files; it does not connect
to an arbitrary external database.

During import, the API:

1. Checks file types, size, row, table, and column limits and reads the tabular data.
2. Creates tables in a generated, isolated PostgreSQL schema.
3. Looks for unique, non-null primary keys such as `id`, `port_id`, or `route_id`.
4. Infers foreign keys from matching `*_id` columns, such as `origin_port_id` to `ports.port_id`, only if
   types match and every non-null value has a matching target key.
5. Grants the query-only database role `SELECT` access and registers the dataset for the selector.

After sign-in, the schema view reads actual database metadata. It displays columns and types, marks PK/FK
fields, and draws labeled lines for foreign keys. The diagram adapts to different table counts and field
widths while retaining the Fallout theme. If an import predates key-inference changes, import its files
again as a new dataset; existing imports are not automatically updated. A missing line means no validated
foreign-key constraint was created. Although admins can inspect the dataset list and manage `core` grants,
imported datasets currently have no deletion or per-user grant UI.

## 5. What happens when someone asks a question

```text
Browser
  → same-origin frontend/API proxy
  → FastAPI checks session, role, and dataset access
  → schema context + glossary help the SQL generator
  → SQL AST policy checks generated SQL
  → API checks every referenced table against role/dataset access
  → PostgreSQL read-only role executes the query with a timeout
  → sanity checks, candidate agreement, and judge back-translation
  → confidence policy chooses answer, warn, or abstain
  → result and permitted SQL return to the browser
```

The generator's SQL is treated as untrusted. Non-approved queries are rejected before execution. Agreement
and correction candidates go through the authorization and SQL checks too. If confidence is too low, the app
can abstain and hide the selected SQL and result rows. Query history stores the question, strategy, SQL,
decision, and confidence metadata for the user; it does not retain result rows.

The UI offers three strategies: schema plus glossary (`schema_glossary`, default), schema only (`zero_shot`),
and schema plus glossary with execution-error correction (`schema_glossary_selfcorrect`). Agreement uses
three candidate generations at different temperatures. Back-translation, agreement, result sanity, schema
coverage, and model confidence feed the final answer/warn/abstain decision.

The query executes with a separate PostgreSQL account that is read-only, in a read-only transaction, with a
statement timeout. The API's administrative database connection handles schema/auth management and is not
used to run generated SQL.

## 6. How the data is protected (and what is not protected)

Protection layers include Argon2 password hashes, hashed session tokens, server-side role checks, `core`
dataset grants, table allowlists, SQL AST policy, the read-only database role, and a query timeout. The
schema view is informational: seeing a table does not authorize querying it. Admin controls are separate
from query execution and do not make generated SQL writable.

This is a local/demo project, not a production security boundary or audited service. Important limits:

- Imported datasets are shared with all signed-in users; there are no per-user imported-dataset grants,
  row-level rules, per-user column rules, or general PII redaction.
- A permitted `SELECT` can return data available to that role. Read-only prevents writes; it does not make
  data private.
- Questions and schema context are sent to the configured external model provider.
- Use HTTPS and set `AUTH_COOKIE_SECURE=true` when serving beyond a trusted local machine. Keep `.env`, local
  account workbooks, and database backups private.
- Login is rate-limited, but query requests have no per-user rate or cost quota. Keep the demo on a trusted
  network and do not upload confidential data.

## 7. Main code locations

| Path | What it does |
| --- | --- |
| `frontend/index.html` | Login, dataset picker/import form, query screen, role UI, and schema diagram |
| `frontend/styles.css` | Fallout theme and layout |
| `frontend/app.py` | Serves the UI and proxies browser API calls |
| `src/t2s/api/main.py` | Authentication/API endpoints, dataset import, authorization, and request workflow |
| `src/t2s/auth/` | Passwords, sessions, role permissions, grants, audit events, and admin bootstrap |
| `src/t2s/schema/` | Database schema models, introspection, retrieval, and glossary |
| `src/t2s/generation/` | Prompt construction and SQL generation |
| `src/t2s/guardrails/` | SQL parsing and policy enforcement |
| `src/t2s/sandbox/` | Read-only SQL execution |
| `src/t2s/verification/` | Sanity checks, agreement, confidence, and abstention |
| `docker/` | PostgreSQL schema, seed data, and read-only role setup |

