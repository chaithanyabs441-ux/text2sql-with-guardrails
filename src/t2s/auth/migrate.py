"""Apply additive auth tables and register the existing core dataset."""

from t2s.auth.store import AuthStore


def main() -> None:
    store = AuthStore()
    store.ensure_schema()
    print("Authentication tables are ready; registered dataset: core.")


if __name__ == "__main__":
    main()
