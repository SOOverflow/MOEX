# -*- coding: utf-8 -*-
"""
Форматирует CSV-файл свечей в XLSX с отдельными столбцами.

Использование:
    python format_csv.py <имя_файла.csv>

Пример:
    python format_csv.py RU000A10AZ60_1min_2026-09-02_2026-10-01.csv

Результат:
    RU000A10AZ60_1min_2026-09-02_2026-10-01.xlsx
"""

import sys
import os
import pandas as pd


def format_csv(input_file: str, log_func=None) -> str:
    """
    Конвертирует CSV в XLSX с автошириной колонок.
    Возвращает путь к XLSX-файлу или None при ошибке.
    log_func — необязательная функция для логирования (например, print).
    """
    def log(msg):
        if log_func:
            log_func(msg)
        else:
            print(msg)

    if not os.path.exists(input_file):
        log(f"Файл не найден: {input_file}")
        return None

    log(f"Чтение CSV: {input_file}...")
    df = pd.read_csv(input_file)

    output_file = os.path.splitext(input_file)[0] + ".xlsx"

    log(f"Конвертация в XLSX: {output_file}...")
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Candles")
        worksheet = writer.sheets["Candles"]
        for idx, col in enumerate(df.columns):
            max_len = max(
                df[col].astype(str).map(len).max(),
                len(col)
            )
            worksheet.column_dimensions[
                worksheet.cell(row=1, column=idx + 1).column_letter
            ].width = max_len + 2

    log(f"Готово! Файл сохранён: {output_file}")
    log(f"Строк: {len(df)} | Колонок: {len(df.columns)}")
    log(f"Колонки: {list(df.columns)}")

    return output_file


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python format_csv.py <имя_файла.csv>")
        sys.exit(1)
    format_csv(sys.argv[1])
