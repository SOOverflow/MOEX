# -*- coding: utf-8 -*-
"""
Создаёт ярлык MOEX.lnk с кастомной иконкой, указывающий на launcher.pyw.
Запуск: python create_shortcut.py (из venv или системного Python)
"""

import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(BASE_DIR, "app_icon.ico")
TARGET = os.path.join(BASE_DIR, "launcher.pyw")
SHORTCUT = os.path.join(BASE_DIR, "MOEX Экспорт свечей.lnk")

# PowerShell-скрипт для создания .lnk
ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut("{SHORTCUT}")
$shortcut.TargetPath = "{TARGET}"
$shortcut.IconLocation = "{ICON_PATH}"
$shortcut.WorkingDirectory = "{BASE_DIR}"
$shortcut.Description = "MOEX — Экспорт свечей облигаций"
$shortcut.Save()
Write-Host "Ярлык создан: {SHORTCUT}"
"""

if not os.path.exists(ICON_PATH):
    print(f"Ошибка: иконка не найдена: {ICON_PATH}")
    sys.exit(1)

if not os.path.exists(TARGET):
    print(f"Ошибка: launcher.pyw не найден: {TARGET}")
    sys.exit(1)

result = subprocess.run(
    ["powershell", "-NoProfile", "-Command", ps_script],
    capture_output=True, text=True
)

if result.returncode == 0:
    print(result.stdout.strip())
    print("Готово! Ярлык создан с кастомной иконкой.")
else:
    print(f"Ошибка PowerShell: {result.stderr}")
    sys.exit(1)
