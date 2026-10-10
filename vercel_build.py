"""
Vercel build hook (see pyproject.toml [tool.vercel.scripts]).
Runs DB migrations, then files the bot's knowledge, when DATABASE_URL is available at build time.
"""
import os
import subprocess
import sys


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("vercel_build: DATABASE_URL not set; skipping migrate.")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    print("vercel_build: running migrate --noinput")
    subprocess.check_call([sys.executable, "manage.py", "migrate", "--noinput"], cwd=here)

    # The bot's knowledge (apps/wahub/knowledge_seed.py) is filed on every deploy:
    # a record that is already there is left alone, so this adds nothing after
    # the first run and never touches the owner's edits. The owner wanted the
    # knowledge simply present, with nothing to click (10.10.2026). It sends
    # nothing. A failure here is reported, not fatal: the deploy must not stall
    # on a seed that the next deploy can file again.
    print("vercel_build: filing the bot's knowledge (wahub_import_old_knowledge)")
    try:
        subprocess.check_call([sys.executable, "manage.py", "wahub_import_old_knowledge"], cwd=here)
    except subprocess.CalledProcessError as exc:
        print(f"vercel_build: WARNING the knowledge import failed (exit {exc.returncode}); the deploy goes on.")


if __name__ == "__main__":
    main()
