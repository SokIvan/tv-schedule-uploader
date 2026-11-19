"""Распознавание трёх файлов расписания в папке."""
import os
import re
from datetime import datetime


NIKA_FILENAME_PATTERN = r"^nika_data_(\d{2})(\d{2})(\d{4})_(\d{2})(\d{2})(\d{2})\.js$"
_NIKA_RE = re.compile(NIKA_FILENAME_PATTERN)


def parse_nika_name(filename: str):
    """Вернуть datetime из имени nika_data_ДДММГГГГ_ЧЧММСС.js или None."""
    m = _NIKA_RE.match(filename)
    if not m:
        return None
    d, mo, y, hh, mm, ss = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


def find_latest_nika(folder: str):
    """Самый свежий по дате в имени nika_data_*.js в папке."""
    best_path = None
    best_dt = None
    try:
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            dt = parse_nika_name(name)
            if dt is None:
                continue
            if best_dt is None or dt > best_dt:
                best_dt = dt
                best_path = full
    except OSError:
        return None
    return best_path


def find_files_in_folder(folder: str) -> dict:
    """
    Просканировать папку и вернуть:
        {"html": path|None, "panel": path|None, "data": path|None}
    Правила:
      - panel: ровно schedule_panel.js (регистронезависимо)
      - html:  самый свежий по mtime *.html
      - data:  самый свежий по дате в имени nika_data_*.js
    """
    if not os.path.isdir(folder):
        raise FileNotFoundError(folder)

    result = {"html": None, "panel": None, "data": None}
    htmls = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        low = name.lower()
        if low == "schedule_panel.js":
            result["panel"] = full
        elif low.endswith(".html") or low.endswith(".htm"):
            htmls.append(full)
    if htmls:
        result["html"] = max(htmls, key=lambda p: os.path.getmtime(p))
    result["data"] = find_latest_nika(folder)
    return result