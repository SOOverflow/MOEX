# -*- coding: utf-8 -*-
"""
Веб-приложение для экспорта свечей облигаций с Московской биржи (MOEX ISS API).

Установка:
    pip install flask requests pandas openpyxl

Запуск:
    python app.py

Откройте в браузере: http://127.0.0.1:5000
"""

from flask import Flask, request, render_template_string, send_file, jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import requests
import pandas as pd
import time as _time
import random
import threading
import os
import shutil
from datetime import datetime
import io

from format_csv import format_csv

app = Flask(__name__)

# =========================================================================
# HTTP-сессия с retry и задержками
# =========================================================================

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36"
    })
    return session

HTTP_SESSION = create_session()

# =========================================================================
# Папка для промежуточных CSV-файлов
# =========================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================================================================
# Глобальное состояние для фоновых задач и логов
# =========================================================================

_task_state = {
    "logs": [],
    "status": "idle",          # idle | running | done | error | stopped
    "progress": 0,
    "file_path": None,
    "error": None,
}
_stop_event = threading.Event()
_task_lock = threading.Lock()


def add_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _task_lock:
        _task_state["logs"].append(f"[{ts}] {msg}")
        if len(_task_state["logs"]) > 200:
            _task_state["logs"] = _task_state["logs"][-200:]


# =========================================================================
# MOEX ISS API логика
# =========================================================================

DIRECT_INTERVALS = {1: 1, 10: 10, 60: 60}
AGGREGATED_INTERVALS = {5, 15, 20, 30}

ALL_INTERVALS = {1: "1 минута", 5: "5 минут", 10: "10 минут",
                 15: "15 минут", 20: "20 минут", 30: "30 минут", 60: "60 минут (1 час)"}


def fetch_candles(secid, moex_interval, start_date, end_date):
    base_url = (
        f"https://iss.moex.com/iss/engines/stock"
        f"/markets/bonds/securities/{secid}/candles.json"
    )
    all_rows = []
    columns = []
    start = 0
    page_num = 0

    while True:
        if _stop_event.is_set():
            raise RuntimeError("Процесс остановлен пользователем")

        params = {
            "from": start_date,
            "till": end_date,
            "interval": moex_interval,
            "start": start,
            "iss.meta": "off",
        }
        try:
            r = HTTP_SESSION.get(base_url, params=params, timeout=(10, 30))
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Ошибка запроса к MOEX: {e}")

        data = r.json()
        candles = data.get("candles", {})
        columns = candles.get("columns", [])
        rows = candles.get("data", [])

        if not rows:
            break

        all_rows.extend(rows)
        start += len(rows)
        page_num += 1
        add_log(f"Страница {page_num}: получено {len(rows)} свечей, всего {len(all_rows)}")

        if len(rows) < 500:
            break

        delay = random.uniform(1.0, 2.0)
        _time.sleep(delay)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=columns)
    df["begin"] = pd.to_datetime(df["begin"])
    df["end"] = pd.to_datetime(df["end"])
    return df


def aggregate_candles(df, target_minutes):
    if df.empty:
        return df

    df = df.set_index("begin")

    agg_map = {}
    for col in ["open", "high", "low", "close", "volume", "value"]:
        if col in df.columns:
            agg_map[col] = {"open": "first", "high": "max", "low": "min",
                            "close": "last", "volume": "sum", "value": "sum"}[col]

    resampled = df.resample(f"{target_minutes}min").agg(agg_map)
    resampled = resampled.dropna(subset=["open"])

    if not resampled.empty:
        resampled["end"] = resampled.index + pd.Timedelta(minutes=target_minutes) - pd.Timedelta(seconds=1)

    return resampled.reset_index()


def process_request(isin, target_interval, start_date, end_date):
    if target_interval in DIRECT_INTERVALS:
        add_log(f"Запрос напрямую с MOEX (interval={target_interval} мин)...")
        df = fetch_candles(isin, DIRECT_INTERVALS[target_interval], start_date, end_date)
        source = f"напрямую с MOEX (interval={target_interval})"
    else:
        add_log(f"Запрос 1-минутных свечей для агрегации в {target_interval} мин...")
        df = fetch_candles(isin, 1, start_date, end_date)
        if _stop_event.is_set():
            raise RuntimeError("Процесс остановлен пользователем")
        add_log(f"Агрегация в {target_interval}-минутные свечи...")
        df = aggregate_candles(df, target_interval)
        source = f"агрегация из 1-минутных свечей в {target_interval}-минутные"

    if df.empty:
        return None, (
            f"Нет данных по ISIN {isin} за период {start_date} — {end_date}. "
            "Возможно, облигация не торговалась в этот период, или ISIN указан неверно."
        )

    output_cols = ["begin", "open", "high", "low", "close"]
    available = [c for c in output_cols if c in df.columns]
    df = df[available].copy()
    df = df.sort_values("begin").reset_index(drop=True)

    info = (
        f"Свечей: {len(df)} | Период: {df['begin'].min()} — {df['begin'].max()} | "
        f"Источник: {source}"
    )
    return df, info


# =========================================================================
# Фоновая задача: скачать -> CSV -> XLSX -> удалить CSV
# =========================================================================

def run_pipeline(isin, target_interval, start_date, end_date):
    with _task_lock:
        _task_state["status"] = "running"
        _task_state["error"] = None
        _task_state["file_path"] = None
        _task_state["progress"] = 0
        _task_state["logs"] = []

    _stop_event.clear()
    add_log(f"Запуск: ISIN={isin}, интервал={target_interval} мин, период={start_date} — {end_date}")

    try:
        df, info = process_request(isin, target_interval, start_date, end_date)

        if _stop_event.is_set():
            raise RuntimeError("Процесс остановлен пользователем")

        if df is None:
            raise RuntimeError(info)

        add_log(f"Данные получены. {info}")
        with _task_lock:
            _task_state["progress"] = 50

        csv_filename = f"{isin}_{target_interval}min_{start_date}_{end_date}.csv"
        csv_path = os.path.join(OUTPUT_DIR, csv_filename)

        add_log(f"Сохранение CSV: {csv_filename}...")
        df.to_csv(csv_path, index=False)

        if _stop_event.is_set():
            _cleanup_csv(csv_path)
            raise RuntimeError("Процесс остановлен пользователем")

        add_log("Конвертация CSV в XLSX...")
        xlsx_path = format_csv(csv_path, log_func=add_log)

        if xlsx_path and os.path.exists(xlsx_path):
            add_log(f"XLSX создан: {os.path.basename(xlsx_path)}")
            with _task_lock:
                _task_state["progress"] = 100
                _task_state["file_path"] = xlsx_path
        else:
            raise RuntimeError("Не удалось создать XLSX-файл")

        _cleanup_csv(csv_path)
        add_log("CSV удалён. Процесс завершён.")
        with _task_lock:
            _task_state["status"] = "done"

    except RuntimeError as e:
        msg = str(e)
        add_log(f"ОШИБКА: {msg}")
        with _task_lock:
            _task_state["status"] = "stopped" if "остановлен" in msg.lower() else "error"
            _task_state["error"] = msg
    except Exception as e:
        add_log(f"ОШИБКА: {e}")
        with _task_lock:
            _task_state["status"] = "error"
            _task_state["error"] = str(e)


def _cleanup_csv(csv_path):
    try:
        if os.path.exists(csv_path):
            os.remove(csv_path)
            add_log(f"Промежуточный CSV удалён: {os.path.basename(csv_path)}")
    except Exception as e:
        add_log(f"Не удалось удалить CSV: {e}")


# =========================================================================
# HTML шаблон
# =========================================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Экспорт свечей MOEX</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f1923;
            color: #d1d4dc;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            padding: 40px 20px;
        }
        .container { max-width: 640px; width: 100%; }
        h1 { font-size: 24px; margin-bottom: 8px; color: #fff; }
        .subtitle { color: #787b86; margin-bottom: 32px; font-size: 14px; line-height: 1.5; }
        .card {
            background: #1e222d;
            border: 1px solid #2a2e39;
            border-radius: 12px;
            padding: 28px;
        }
        label { display: block; font-size: 13px; color: #787b86; margin-bottom: 6px; font-weight: 600; }
        input[type="text"], input[type="date"], select {
            width: 100%;
            padding: 12px 14px;
            background: #0f1923;
            border: 1px solid #2a2e39;
            border-radius: 8px;
            color: #d1d4dc;
            font-size: 15px;
            margin-bottom: 20px;
            transition: border-color 0.2s;
        }
        input:focus, select:focus { outline: none; border-color: #2962ff; }
        .row { display: flex; gap: 16px; }
        .row > div { flex: 1; }
        .btn-row { display: flex; gap: 12px; margin-top: 4px; }
        .btn-row button { flex: 1; }
        button {
            width: 100%;
            padding: 14px;
            background: #2962ff;
            color: #fff;
            border: none;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        button:hover:not(:disabled) { background: #1e53e5; }
        button:disabled { background: #2a2e39; color: #787b86; cursor: not-allowed; }
        .btn-stop {
            background: #ef5350 !important;
        }
        .btn-stop:hover:not(:disabled) { background: #e53935 !important; }
        .btn-download {
            background: #26a69a !important;
        }
        .btn-download:hover:not(:disabled) { background: #2bbbad !important; }
        .btn-open {
            background: #2962ff !important;
        }
        .btn-open:hover:not(:disabled) { background: #1e53e5 !important; }
        .info-box {
            margin-top: 16px;
            padding: 14px;
            background: #131722;
            border: 1px solid #2a2e39;
            border-radius: 8px;
            font-size: 13px;
            color: #787b86;
            line-height: 1.6;
        }
        .warning {
            margin-top: 12px;
            padding: 12px 14px;
            background: rgba(255, 193, 7, 0.08);
            border: 1px solid rgba(255, 193, 7, 0.2);
            border-radius: 8px;
            font-size: 13px;
            color: #ffc107;
            line-height: 1.5;
        }
        #loading { display: none; text-align: center; padding: 30px 0; }
        .spinner {
            width: 36px; height: 36px; margin: 0 auto 12px;
            border: 3px solid #2a2e39; border-top: 3px solid #2962ff;
            border-radius: 50%; animation: spin 1s linear infinite;
        }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        #status-text { color: #787b86; font-size: 14px; }
        .progress-bar {
            width: 100%; max-width: 300px; margin: 12px auto 0;
            background: #2a2e39; border-radius: 4px; overflow: hidden;
        }
        .progress-fill {
            height: 6px; background: #2962ff; width: 0%;
            transition: width 0.3s ease;
        }
        .log-console {
            margin-top: 20px;
            background: #0a0a0a;
            border: 1px solid #2a2e39;
            border-radius: 8px;
            padding: 12px;
            max-height: 240px;
            overflow-y: auto;
            font-family: "Cascadia Code", "Consolas", "Courier New", monospace;
            font-size: 12px;
            line-height: 1.6;
        }
        .log-line { color: #4caf50; }
        .log-info { color: #42a5f5; }
        .log-error { color: #ef5350; }
        .result-box {
            margin-top: 20px;
            padding: 16px;
            background: rgba(38, 166, 154, 0.08);
            border: 1px solid rgba(38, 166, 154, 0.2);
            border-radius: 8px;
            color: #26a69a;
            font-size: 14px;
        }
        .error-box {
            margin-top: 20px;
            padding: 16px;
            background: rgba(239, 83, 80, 0.08);
            border: 1px solid rgba(239, 83, 80, 0.2);
            border-radius: 8px;
            color: #ef5350;
            font-size: 14px;
        }
        .btn-group { display: flex; gap: 12px; margin-top: 12px; }
        .btn-group button { flex: 1; }
        .footer { margin-top: 24px; text-align: center; color: #4c525e; font-size: 12px; }
    </style>
</head>
<body>
<div class="container">
    <h1>Экспорт свечей облигаций · MOEX</h1>
    <p class="subtitle">
        Скачивание исторических свечей с Московской биржи через ISS API.
        Доступные интервалы: 1, 5, 10, 15, 20, 30, 60 минут.
    </p>

    <div class="card">
        <form id="form" onsubmit="return false;">
            <label for="isin">ISIN облигации</label>
            <input type="text" id="isin" name="isin" value="RU000A10AZ60" placeholder="RU000A10AZ60" required>

            <label for="interval">Интервал свечей</label>
            <select id="interval" name="interval">
                {% for val, label in intervals.items() %}
                <option value="{{ val }}" {% if val == 5 %}selected{% endif %}>{{ label }}</option>
                {% endfor %}
            </select>

            <div class="row">
                <div>
                    <label for="start_date">Начало периода</label>
                    <input type="date" id="start_date" name="start_date" value="2025-10-02" required>
                </div>
                <div>
                    <label for="end_date">Конец периода</label>
                    <input type="date" id="end_date" name="end_date" value="2026-10-02" required>
                </div>
            </div>

            <div class="btn-row">
                <button type="button" id="btn-start" onclick="startProcess()">Скачать данные</button>
                <button type="button" id="btn-stop" class="btn-stop" onclick="stopProcess()" disabled>Stop</button>
            </div>

            <div class="warning" id="warn" style="display:none;">
                Интервалы 5, 15, 20 и 30 минут требуют скачивания всех 1-минутных свечей
                с последующей агрегацией. За год это может занять несколько минут.
            </div>
        </form>

        <div id="loading">
            <div class="spinner"></div>
            <p id="status-text">Ожидание...</p>
            <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
        </div>

        <div class="log-console" id="log-console" style="display:none;"></div>

        <div id="result-area"></div>
    </div>

    <div class="info-box">
        <strong>Как это работает:</strong><br>
        1. Запрос отправляется на ISS API Московской биржи (без авторизации).<br>
        2. Для интервалов 5, 15, 20, 30 мин скачиваются минутные свечи и агрегируются.<br>
        3. Данные скачиваются страницами по 500 свечей (пагинация).<br>
        4. CSV автоматически конвертируется в XLSX, промежуточный CSV удаляется.<br>
        5. В файле только 5 колонок: begin, open, high, low, close.
    </div>

    <div class="footer">Данные: iss.moex.com · Без авторизации</div>
</div>

<script>
const btnStart = document.getElementById('btn-start');
const btnStop = document.getElementById('btn-stop');
const loading = document.getElementById('loading');
const statusText = document.getElementById('status-text');
const progressFill = document.getElementById('progress-fill');
const resultArea = document.getElementById('result-area');
const logConsole = document.getElementById('log-console');
const warnBox = document.getElementById('warn');
const intervalSelect = document.getElementById('interval');

let pollTimer = null;
let lastLogLen = 0;

function updateWarning() {
    const v = parseInt(intervalSelect.value);
    warnBox.style.display = [5, 15, 20, 30].includes(v) ? 'block' : 'none';
}
intervalSelect.addEventListener('change', updateWarning);
updateWarning();

function startProcess() {
    const isin = document.getElementById('isin').value.trim();
    const interval = intervalSelect.value;
    const startDate = document.getElementById('start_date').value;
    const endDate = document.getElementById('end_date').value;

    if (!isin) { alert('Укажите ISIN облигации'); return; }
    if (!startDate || !endDate) { alert('Укажите период'); return; }

    btnStart.disabled = true;
    btnStop.disabled = false;
    loading.style.display = 'block';
    logConsole.style.display = 'block';
    logConsole.innerHTML = '';
    resultArea.innerHTML = '';
    lastLogLen = 0;
    statusText.textContent = 'Запуск...';
    progressFill.style.width = '0%';

    fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ isin, interval, start_date: startDate, end_date: endDate })
    }).then(r => r.json()).then(data => {
        if (data.error) {
            resultArea.innerHTML = '<div class="error-box">' + data.error + '</div>';
            btnStart.disabled = false;
            btnStop.disabled = true;
            loading.style.display = 'none';
        } else {
            pollStatus();
        }
    }).catch(err => {
        resultArea.innerHTML = '<div class="error-box">Ошибка: ' + err.message + '</div>';
        btnStart.disabled = false;
        btnStop.disabled = true;
        loading.style.display = 'none';
    });
}

function stopProcess() {
    fetch('/api/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            statusText.textContent = 'Остановка...';
        });
}

function openXlsx() {
    fetch('/api/open_xlsx')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                alert(data.error);
            }
        })
        .catch(err => alert('Ошибка: ' + err.message));
}

function saveXlsx() {
    fetch('/api/save_xlsx')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                alert(data.error);
            } else if (data.saved_path) {
                alert('Файл сохранён: ' + data.saved_path);
            }
        })
        .catch(err => alert('Ошибка: ' + err.message));
}

function pollStatus() {
    pollTimer = setInterval(() => {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                if (data.logs.length > lastLogLen) {
                    const newLogs = data.logs.slice(lastLogLen);
                    newLogs.forEach(line => {
                        const div = document.createElement('div');
                        div.className = 'log-line';
                        if (line.includes('ОШИБКА')) {
                            div.className += ' log-error';
                        } else if (line.includes('Страница') || line.includes('Сохранение') || line.includes('Конвертация') || line.includes('завершён')) {
                            div.className += ' log-info';
                        }
                        div.textContent = line;
                        logConsole.appendChild(div);
                    });
                    lastLogLen = data.logs.length;
                    logConsole.scrollTop = logConsole.scrollHeight;
                }

                if (data.progress > 0) {
                    progressFill.style.width = data.progress + '%';
                }

                statusText.textContent = 'Выполняется... ' + data.progress + '%';

                if (data.status === 'done') {
                    clearInterval(pollTimer);
                    btnStart.disabled = false;
                    btnStop.disabled = true;
                    loading.style.display = 'none';
                    progressFill.style.width = '100%';
                    if (data.file_path) {
                        const fn = data.file_path.split(/[/\\\\]/).pop();
                        resultArea.innerHTML =
                            '<div class="result-box">Файл готов: ' + fn + '</div>' +
                            '<div class="btn-group">' +
                            '<button type="button" class="btn-download" onclick="saveXlsx()">Сохранить как...</button>' +
                            '<button type="button" class="btn-open" onclick="openXlsx()">Открыть в Excel</button>' +
                            '</div>';
                    }
                } else if (data.status === 'error' || data.status === 'stopped') {
                    clearInterval(pollTimer);
                    btnStart.disabled = false;
                    btnStop.disabled = true;
                    loading.style.display = 'none';
                    resultArea.innerHTML = '<div class="error-box">' + (data.error || 'Процесс остановлен') + '</div>';
                }
            });
    }, 700);
}
</script>
</body>
</html>
"""

# =========================================================================
# Маршруты
# =========================================================================

@app.route('/')
def index():
    return render_template_string(HTML_PAGE, intervals=ALL_INTERVALS)


@app.route('/api/start', methods=['POST'])
def api_start():
    isin = request.form.get('isin', '').strip()
    interval_str = request.form.get('interval', '5')
    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')

    if not isin:
        return {"error": "Укажите ISIN облигации"}
    try:
        target_interval = int(interval_str)
    except ValueError:
        return {"error": "Некорректный интервал"}

    if not start_date or not end_date:
        return {"error": "Укажите период (даты начала и окончания)"}

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"error": "Некорректный формат даты. Используйте ГГГГ-ММ-ДД"}

    if start_date > end_date:
        return {"error": "Дата начала больше даты окончания"}

    with _task_lock:
        if _task_state["status"] == "running":
            return {"error": "Процесс уже запущен. Дождитесь завершения или нажмите Stop."}

    with _task_lock:
        _task_state["status"] = "running"
        _task_state["logs"] = []
        _task_state["progress"] = 0
        _task_state["file_path"] = None
        _task_state["error"] = None

    thread = threading.Thread(
        target=run_pipeline,
        args=(isin, target_interval, start_date, end_date),
        daemon=True
    )
    thread.start()

    return {"ok": True}


@app.route('/api/stop', methods=['POST'])
def api_stop():
    _stop_event.set()
    add_log("Получен сигнал остановки...")
    return {"ok": True}


@app.route('/api/status')
def api_status():
    with _task_lock:
        return {
            "status": _task_state["status"],
            "progress": _task_state["progress"],
            "logs": list(_task_state["logs"]),
            "file_path": _task_state["file_path"],
            "error": _task_state["error"],
        }


@app.route('/api/download_file')
def api_download_file():
    with _task_lock:
        file_path = _task_state["file_path"]

    if not file_path or not os.path.exists(file_path):
        return "Файл не найден. Повторите запрос.", 404

    return send_file(file_path, as_attachment=True)


@app.route('/api/open_xlsx')
def api_open_xlsx():
    """Открывает XLSX-файл в приложении по умолчанию (Excel)."""
    with _task_lock:
        file_path = _task_state["file_path"]

    if not file_path or not os.path.exists(file_path):
        return {"error": "Файл не найден. Повторите запрос."}, 404

    try:
        os.startfile(file_path)
        add_log(f"Файл открыт: {os.path.basename(file_path)}")
        return {"ok": True}
    except Exception as e:
        add_log(f"Не удалось открыть файл: {e}")
        return {"error": f"Не удалось открыть файл: {e}"}, 500


@app.route('/api/save_xlsx')
def api_save_xlsx():
    """Показывает диалог 'Сохранить как...' и копирует файл."""
    with _task_lock:
        file_path = _task_state["file_path"]

    if not file_path or not os.path.exists(file_path):
        return {"error": "Файл не найден. Повторите запрос."}, 404

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        original_name = os.path.basename(file_path)
        save_path = filedialog.asksaveasfilename(
            title="Сохранить файл как...",
            initialfile=original_name,
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")]
        )
        root.destroy()

        if not save_path:
            return {"ok": False, "message": "Сохранение отменено"}

        shutil.copy2(file_path, save_path)
        add_log(f"Файл сохранён: {save_path}")
        return {"ok": True, "saved_path": save_path}

    except Exception as e:
        add_log(f"Не удалось сохранить файл: {e}")
        return {"error": f"Не удалось сохранить файл: {e}"}, 500


# =========================================================================
# Запуск
# =========================================================================

if __name__ == '__main__':
    print("=" * 50)
    print("  Экспорт свечей MOEX — веб-приложение запущено")
    print("  Откройте в браузере: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)
