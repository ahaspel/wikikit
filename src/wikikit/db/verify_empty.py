"""Verify database is empty. Exits with code 1 if not."""
import sys
from wikikit.db.session import SessionLocal
from wikikit.db.models import Article


def main() -> None:
    session = SessionLocal()
    count = session.query(Article).count()
    session.close()

    if count != 0:
        print(f"ERROR: Database not empty ({count} articles). Aborting.")
        sys.exit(1)
    print("Verified: database empty.")


# Guarded: this is a package module now, and importing a module must never act.
if __name__ == "__main__":
    main()
