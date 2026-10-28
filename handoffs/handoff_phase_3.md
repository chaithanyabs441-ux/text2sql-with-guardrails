# SafeQuery AI — Phase 3 handoff

Date: 27 October 2026  
Workspace: `/home/death-kid/IDE/text2sql-with-guardrails`

This handoff records the latest schema-diagram, dataset-import, role, and security documentation updates.
Phase 2 remains useful for the initial Docker/auth implementation; where its import-key instructions differ,
this Phase 3 note describes the newer behavior. Phase 1 is an earlier machine-specific setup record.

## Current local status

After the API rebuild for key inference, `docker compose ps` showed PostgreSQL and the API healthy, with the
frontend running. `http://localhost:8000/health` returned `{"status":"ok"}`. The API was rebuilt from the
current source; the PostgreSQL data volume was preserved. The current maritime dataset was not reimported
as part of this change.

Start the stack with `make`. The Makefile prints a clickable `http://localhost:8501` link, plus API docs and
health URLs. Common commands: `make status`, `make logs`, `make follow`, and `make down`. `make down` preserves
the database volume; avoid `docker compose down -v` unless you intend to delete all local database data.

## Phase 3 changes

### Imported primary and foreign keys

`src/t2s/api/main.py` now infers more than the conventional `id` naming pattern when importing CSV files or
Excel sheets:

- A table can use a unique, non-null entity key such as `ports.port_id`, `routes.route_id`, or
  `vessels.vessel_id`. Conventional `id` remains supported. A lone `*_id` column can also be used when the
  table/file name does not match its entity name.
- Reference columns are matched to imported primary keys by key suffix, including prefixed references such
  as `routes.origin_port_id` and `routes.destination_port_id` to `ports.port_id`.
- A foreign key is created only when the database types are compatible and every non-null source value
  exists in the target key. This prevents drawing relationships that fail validation. A field that does not
  validate remains a regular queryable column without a PK/FK badge or connector.
- The schema introspector reads actual PostgreSQL constraints. The ER diagram marks PK/FK columns and draws
  labeled relationship lines from those constraints.

The diagram renderer also sizes the type, column-name, and key columns to their content, uses taller rows,
centers a single-table import, and lays multi-table imports out in an adaptive grid. It retains the existing
Fallout visual theme. The sizing is generic and applies to uploaded schemas such as the 31-column
`creditcard.csv` and the maritime tables.

**Existing uploads are not migrated automatically.** To get the new key badges and relationship lines,
reimport the source CSV files together (or the workbook sheets) as a new dataset, then select that new dataset
and refresh the schema. Each CSV is one table; the importer does not load sibling files unless they are
selected in the import form. Imports are shared with all signed-in accounts, so do not use confidential data.

### Role responsibilities and data path

Roles are read from the server-side user record and enforced by API permission checks. The browser controls
which options are visible; it is not the authorization boundary.

| Role | `core` query scope | Other permissions |
| --- | --- | --- |
| Employee | Seven operational tables: `regions`, `categories`, `products`, `customers`, `orders`, `order_items`, and `support_tickets` | View schema and own query history |
| Analyst | All ten `core` tables, including `employees`, `payments`, and `returns` | View own history and query activity analytics |
| Admin | All ten `core` tables | Manage users/roles, activate/deactivate accounts, reset passwords, grant/revoke `core` access, and view audit logs and all query activity |

Imported datasets are intentionally available to all signed-in roles, with every imported table queryable by
each role. The import form is available before sign-in, so uploads are shared server-wide, not private to
their uploader. The diagram shows all tables in the selected dataset; for `core`, it marks employee-restricted
tables, but visibility in the diagram does not grant query access.

For a query, the API checks the session, role, dataset access, and every physical SQL table reference before
execution. Candidate SQL also passes the AST policy. Permitted generated queries run through the separate
PostgreSQL `t2s_readonly` role in a read-only transaction with a statement timeout. Admin power to manage
accounts and access does not make generated SQL writable. Auth/session/audit tables are not business query
tables.

### Authentication and data protections

- PostgreSQL-backed authentication uses Argon2 password hashes and opaque session tokens; only a token
  digest is stored server-side. The browser cookie is HTTP-only, SameSite strict, and lasts 12 hours.
- Password reset and account deactivation revoke active sessions; login attempts are rate limited.
- Core dataset access is granted per user. Employee table scope and admin-only endpoints are checked by the
  API, not merely hidden in the interface.
- Generated SQL is constrained to approved read operations and tables, with a separate read-only database
  role and statement timeout.
- The application uses an administrative database connection for schema/auth management, while generated
  SQL executes with the read-only database role. Keep database credentials and ports on a trusted machine
  or network.

These are layered demo protections, not a production security certification. There is no row-level policy,
per-user column policy, general PII redaction, or per-user imported-dataset grant. A permitted `SELECT` may
return every value visible to that role. Imports, prompts, and schema context should not contain confidential
data; question/schema context is sent to the configured external model provider. Use HTTPS and
`AUTH_COOKIE_SECURE=true` if traffic leaves a trusted local machine. The deployment has not had an independent
security audit.

## Main files

| File | Purpose |
| --- | --- |
| `src/t2s/api/main.py` | Dataset import, inferred PK/FK constraints, authorization, and query endpoints |
| `src/t2s/schema/introspector.py` | Reads table, column, PK, and FK metadata from PostgreSQL |
| `frontend/index.html` | Login/import form, role UI, schema diagram, query and admin views |
| `frontend/styles.css` | Fallout styling and adaptive schema diagram text sizing |
| `src/t2s/auth/permissions.py` | Role permissions and core table allowlists |
| `Makefile` | Compose start/stop and clickable local URL output |
| `README.md` | Current setup, roles, import semantics, and security boundaries |
| `docs/design.md` | Trust boundaries and threat model |

## Verification and remaining work

For the latest key-inference change, `python -m py_compile src/t2s/api/main.py` and
`git diff --check -- src/t2s/api/main.py` passed. Docker rebuilt and restarted the API; the health endpoint
returned `{"status":"ok"}` and Docker reported PostgreSQL/API healthy. No test suite was run, and the updated
inference has not yet been exercised by reimporting the maritime CSV files. Reimport them together to check
that entity-specific PK badges and `origin_port_id`/`destination_port_id` relationship connectors appear.

The frontend ER diagram spacing update was rebuilt earlier and served successfully. The broader Python and
JavaScript test suites were not run for these documentation updates.
