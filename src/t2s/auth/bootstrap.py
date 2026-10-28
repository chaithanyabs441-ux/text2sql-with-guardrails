"""Create the first administrator explicitly from environment variables."""

import os

from sqlalchemy.exc import IntegrityError

from t2s.auth.store import AuthStore


def main() -> None:
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        raise SystemExit(
            "Set BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD for this command."
        )
    if len(password) < 12:
        raise SystemExit("BOOTSTRAP_ADMIN_PASSWORD must contain at least 12 characters.")
    store = AuthStore()
    store.ensure_schema()
    if store.get_user_by_name(username):
        raise SystemExit("That username already exists; no changes were made.")
    try:
        user = store.create_user(username, password, "admin")
    except IntegrityError as exc:
        raise SystemExit(
            "Could not create the administrator; check for an existing username."
        ) from exc
    store.grant_dataset(user["id"], "core", user["id"])
    print(f"Created administrator account '{user['username']}'.")


if __name__ == "__main__":
    main()
