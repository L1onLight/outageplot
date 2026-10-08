"""Pre-commit хук: падає, якщо в файлах трапляються значення з .env.

Ловить те, що не впізнає gitleaks: адресу, chat id тощо.
Самі значення не друкуються - лише назва змінної, файл і рядок.
"""

import sys
from pathlib import Path

from dotenv import dotenv_values

MIN_LEN = 4  # коротші значення дають забагато хибних збігів

env_file = Path(__file__).resolve().parent.parent / ".env"
if not env_file.exists():
    sys.exit(0)

secrets = {k: v for k, v in dotenv_values(env_file).items() if v and len(v) >= MIN_LEN}
found = False
for name in sys.argv[1:]:
    try:
        text = Path(name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue  # бінарні файли пропускаємо
    for lineno, line in enumerate(text.splitlines(), 1):
        for key, value in secrets.items():
            if value in line:
                print(f"{name}:{lineno}: містить значення {key} з .env")
                found = True

sys.exit(1 if found else 0)
