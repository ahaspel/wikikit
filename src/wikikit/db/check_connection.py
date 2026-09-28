"""Check database connection. Exits with code 1 if unreachable."""
import sys
from sqlalchemy import text
from wikikit.db.session import SessionLocal


def main() -> None:
    try:
        session = SessionLocal()
        session.execute(text("SELECT 1"))
        session.close()
        print("Database connection OK.")
    except Exception as e:
        print(f"ERROR: Cannot connect to database: {e}")
        sys.exit(1)


# Guarded: this is a package module now, and importing a module must never act.
if __name__ == "__main__":
    main()
