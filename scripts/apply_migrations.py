"""Apply SQL migrations from db/migrations in filename order.

Tracks applied files in a schema_migrations table so reruns are no-ops.
Usage: python scripts/apply_migrations.py
"""

import sys
from pathlib import Path

from onchain_console.db import get_connection

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def main() -> int:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            row[0]
            for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        conn.commit()

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"skip    {path.name} (already applied)")
                continue
            print(f"apply   {path.name}")
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            conn.commit()
    print("migrations up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
