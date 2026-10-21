# -*- coding: utf-8 -*-
"""
Лаунчер: проверяет файлы, venv, зависимости, запускает app.py
и открывает мини-браузер. Закрытие окна останавливает приложение.
"""

import os
import sys
import subprocess
import time
import socket
import ctypes
import threading

MB_OK = 0x00000000
MB_ICONERROR = 0x00000010
MB_ICONINFO = 0x00000040

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(BASE_DIR, "venv")
VENV_PYTHON = os.path.join(VENV_DIR, "Scripts", "python.exe")
VENV_PYTHONW = os.path.join(VENV_DIR, "Scripts", "pythonw.exe")
REQUIRED_FILES = ["app.py", "format_csv.py"]
REQUIRED_PACKAGES = [
    ("flask", "flask"),
    ("requests", "requests"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("pywebview", "pywebview"),
]


def msg(text, title="MOEX Лаунчер", icon=MB_ICONINFO):
    ctypes.windll.user32.MessageBoxW(0, text, title, MB_OK | icon)


def find_system_python():
    """Ищет python.exe через py -launcher или в PATH."""
    try:
        result = subprocess.run(
            ["py", "-3", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    for exe in ["python", "python3"]:
        try:
            result = subprocess.run(
                [exe, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    return None


def create_venv():
    msg("Создаётся виртуальное окружение venv...\nЭто займёт около минуты.", "MOEX Лаунчер")
    py = find_system_python()
    if not py:
        msg("Python не найден!\nУстановите Python 3.10+ и добавьте его в PATH.", "Ошибка", MB_ICONERROR)
        sys.exit(1)
    result = subprocess.run(
        [py, "-m", "venv", VENV_DIR],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        msg(f"Не удалось создать venv:\n{result.stderr}", "Ошибка", MB_ICONERROR)
        sys.exit(1)


def check_package(package_name):
    """Проверяет, установлен ли пакет в venv."""
    result = subprocess.run(
        [VENV_PYTHON, "-c", f"import {package_name}"],
        capture_output=True, text=True, timeout=10
    )
    return result.returncode == 0


def install_packages(packages_to_install):
    """Устанавливает пакеты в venv без вывода консоли."""
    pip_args = [VENV_PYTHON, "-m", "pip", "install"] + packages_to_install
    subprocess.run(pip_args, capture_output=True, text=True, timeout=300)


def wait_for_flask(host="127.0.0.1", port=5000, timeout=60):
    """Ждёт, пока Flask не начнёт отвечать на запросы."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect((host, port))
            sock.close()
            return True
        except (socket.error, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def main():
    # 1. Проверка файлов
    for f in REQUIRED_FILES:
        path = os.path.join(BASE_DIR, f)
        if not os.path.exists(path):
            msg(f"Файл не найден: {f}\nУбедитесь, что все файлы лежат в одной папке.", "Ошибка", MB_ICONERROR)
            sys.exit(1)

    # 2. Проверка venv
    if not os.path.exists(VENV_PYTHON):
        create_venv()

    # 3. Проверка зависимостей
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES:
        if not check_package(import_name):
            missing.append(pip_name)

    if missing:
        msg(f"Устанавливаются зависимости:\n{', '.join(missing)}\n\nПодождите...", "MOEX Лаунчер")
        install_packages(missing)

    # 4. Проверка pywebview отдельно (он нужен лаунчеру, а не app.py)
    result = subprocess.run(
        [VENV_PYTHON, "-c", "import webview"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install", "pywebview"],
            capture_output=True, text=True, timeout=120
        )

    # 5. Запуск app.py в фоне
    app_process = subprocess.Popen(
        [VENV_PYTHONW, os.path.join(BASE_DIR, "app.py")],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    # 6. Ожидание Flask
    if not wait_for_flask():
        app_process.terminate()
        msg("Сервер не запустился за 60 секунд.\nПроверьте app.py.", "Ошибка", MB_ICONERROR)
        sys.exit(1)

    # 7. Открытие мини-браузера через pywebview
    # Создаём временный скрипт, который запустит pywebview внутри venv
    webview_script = os.path.join(BASE_DIR, "_webview_runner.py")
    with open(webview_script, "w", encoding="utf-8") as f:
        f.write(
            "import webview\n"
            "webview.create_window('MOEX — Экспорт свечей облигаций', "
            "'http://127.0.0.1:5000', width=920, height=980, min_size=(700, 600))\n"
            "webview.start()\n"
        )

    try:
        result = subprocess.run(
            [VENV_PYTHONW, webview_script],
            cwd=BASE_DIR,
            timeout=None,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except KeyboardInterrupt:
        pass
    finally:
        # 8. Остановка Flask и очистка
        app_process.terminate()
        try:
            app_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app_process.kill()

        try:
            os.remove(webview_script)
        except OSError:
            pass


if __name__ == "__main__":
    main()
