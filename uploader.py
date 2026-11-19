"""Загрузка трёх файлов на TV + reload."""
import os
import time
import requests
from urllib.parse import quote


def _win_long(path: str) -> str:
    if os.name == "nt" and not path.startswith("\\\\?\\") and len(path) >= 260:
        return "\\\\?\\" + os.path.abspath(path)
    return path


def _request_with_retry(method, url, **kwargs):
    """Одна повторная попытка через 1 сек при сетевой ошибке."""
    try:
        return requests.request(method, url, **kwargs)
    except (requests.ConnectionError, requests.Timeout):
        time.sleep(1)
        return requests.request(method, url, **kwargs)


def _headers(pin, content_type=None):
    h = {}
    if content_type:
        h["Content-Type"] = content_type
    if pin:
        h["X-Pin"] = pin
    return h


def _http_error_text(r):
    try:
        j = r.json()
        return f"HTTP {r.status_code}: {j.get('error') or j}"
    except Exception:
        return f"HTTP {r.status_code}"


def _upload_one(ip, port, local_path, filename, pin, timeout=30):
    """Возвращает (status, detail). status: 'ok' | 'error' | 'pin'."""
    url = f"http://{ip}:{port}/api/schedule-files/upload/{quote(filename)}"
    try:
        with open(_win_long(local_path), "rb") as f:
            payload = f.read()
    except OSError as e:
        return ("error", f"read: {e}")

    try:
        r = _request_with_retry(
            "PUT", url, data=payload,
            headers=_headers(pin, "application/octet-stream"),
            timeout=timeout,
        )
    except requests.RequestException as e:
        return ("error", f"network: {e}")

    if r.status_code == 401:
        return ("pin", None)
    if r.status_code != 200:
        return ("error", _http_error_text(r))
    try:
        j = r.json()
    except ValueError:
        return ("error", "invalid JSON")
    if j.get("ok"):
        return ("ok", j.get("name") or filename)
    return ("error", j.get("error") or "unknown error")


def _reload_tv(ip, port, pin, timeout=15):
    url = f"http://{ip}:{port}/api/schedule-files/reload"
    try:
        r = _request_with_retry(
            "POST", url, json={},
            headers=_headers(pin, "application/json"),
            timeout=timeout,
        )
    except requests.RequestException as e:
        return ("error", f"network: {e}")
    if r.status_code == 401:
        return ("pin", None)
    if r.status_code != 200:
        return ("error", _http_error_text(r))
    try:
        j = r.json()
    except ValueError:
        return ("error", "invalid JSON")
    if j.get("ok"):
        return ("ok", None)
    return ("error", j.get("error") or "reload failed")


def upload_to_tv(tv, files, port, pin, log_fn=None):
    """
    files = {
        'html_path', 'html_name',
        'panel_path', 'panel_name',
        'data_path', 'data_name',
    }
    Возвращает dict результата: {ip, name, ok, error?, pin_required?}
    """
    ip = tv["ip"]
    name = tv.get("name") or ip

    steps = [
        ("HTML", files["html_path"], files["html_name"]),
        ("Panel", files["panel_path"], files["panel_name"]),
        ("Data", files["data_path"], files["data_name"]),
    ]

    for label, path, fname in steps:
        status, msg = _upload_one(ip, port, path, fname, pin)
        if status == "pin":
            return {"ip": ip, "name": name, "ok": False, "pin_required": True}
        if status != "ok":
            if log_fn:
                log_fn(f"{name} ({ip}): {label} — ошибка: {msg}", "ERROR")
            return {"ip": ip, "name": name, "ok": False, "error": f"{label}: {msg}"}
        if log_fn:
            log_fn(f"{name} ({ip}): {label} → {fname} ok")

    status, msg = _reload_tv(ip, port, pin)
    if status == "pin":
        return {"ip": ip, "name": name, "ok": False, "pin_required": True}
    if status != "ok":
        if log_fn:
            log_fn(f"{name} ({ip}): reload — ошибка: {msg}", "ERROR")
        return {"ip": ip, "name": name, "ok": False, "error": f"reload: {msg}"}

    if log_fn:
        log_fn(f"{name} ({ip}): готово", "INFO")
    return {"ip": ip, "name": name, "ok": True}