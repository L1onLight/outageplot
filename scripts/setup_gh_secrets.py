#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["python-dotenv"]
# ///
"""Налаштування секретів GitHub Actions з локального .env через gh.

Друкує команди, які виконає (токен Telegram обрізаний до BOT_ID:...),
питає підтвердження і задає секрети в поточному репозиторії.

Запуск: ./scripts/setup_gh_secrets.py [-y]
"""

import shlex
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

NAMES = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "STREET", "HOUSE"]

env_file = Path(__file__).resolve().parent.parent / ".env"
if not env_file.exists():
    sys.exit(f"Немає {env_file}")

env = dotenv_values(env_file)
missing = [n for n in NAMES if not env.get(n)]
if missing:
    sys.exit(f"У .env бракує: {', '.join(missing)}")


def shown(name: str, value: str) -> str:
    if name == "TELEGRAM_BOT_TOKEN":
        return value.split(":", 1)[0] + ":..."
    return value


for name in NAMES:
    print(f"gh secret set {name} --body {shlex.quote(shown(name, env[name]))}")

if "-y" not in sys.argv[1:]:
    try:
        answer = input("\nЗадати ці секрети? [y/N] ")
    except (EOFError, KeyboardInterrupt):  # Ctrl+D / Ctrl+C
        answer = ""
        print()
    if answer.strip().lower() != "y":
        sys.exit("Скасовано")

for name in NAMES:
    # значення через stdin, щоб не світити його в списку процесів
    subprocess.run(["gh", "secret", "set", name], input=env[name], text=True, check=True)
