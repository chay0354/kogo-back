"""
Vercel build hook (see pyproject.toml [tool.vercel.scripts]).
Runs DB migrations when DATABASE_URL is available at build time.
"""
import os
import subprocess
import sys


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("vercel_build: DATABASE_URL not set; skipping migrate.")
        return
    print("vercel_build: running migrate --noinput")
    subprocess.check_call(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )


if __name__ == "__main__":
    main()
