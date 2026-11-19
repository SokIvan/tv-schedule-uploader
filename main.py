"""TV Schedule Uploader — tkinter UI."""
import os
import re
import queue
import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import Config
from logger import setup_logger
from file_picker_util import (
    find_files_in_folder,
    find_latest_nika,
    parse_nika_name,
)
from scanner import scan_network, ping_one, check_one   # NEW: check_one
from uploader import upload_to_tv


# ─── Цвета ──────────────────────────────────────────────────────────────
BG      = "#070B16"
PANEL   = "#111A33"
ACCENT  = "#7A5CFF"
OK_CLR  = "#4CAF50"
ERR_CLR = "#FF5252"
FG      = "#FFFFFF"
MUTED   = "#9AA4BF"


# NEW: утилиты для разбора IP-ввода
_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _is_valid_ip(ip: str) -> bool:
    if not _IP_RE.match(ip):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def _parse_ip_input(raw: str, default_port: int):
    """Разрешает '1.2.3.4', '1.2.3.4:8080', '1.2.3.4 8080'."""
    raw = (raw or "").strip()
    if not raw:
        return "", default_port
    if ":" in raw:
        host, _, p = raw.partition(":")
        try:
            return host.strip(), int(p.strip())
        except ValueError:
            return host.strip(), default_port
    if " " in raw:
        parts = raw.split(None, 1)
        try:
            return parts[0], int(parts[1])
        except (ValueError, IndexError):
            return parts[0], default_port
    return raw, default_port


# ─── Вспомогательные диалоги ───────────────────────────────────────────
class PinDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Введите PIN")
        self.configure(bg=BG)
        self.geometry("380x210")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        tk.Label(self, text="Телевизор запросил PIN.\nВведите PIN:",
                 bg=BG, fg=FG, font=("Segoe UI", 11),
                 justify="center").pack(pady=(20, 8))
        self.entry = tk.Entry(self, show="•", bg=PANEL, fg=FG,
                              insertbackground=FG, relief="flat",
                              font=("Segoe UI", 13))
        self.entry.pack(fill="x", padx=28, ipady=6)
        self.entry.focus_set()

        row = tk.Frame(self, bg=BG)
        row.pack(pady=18)
        tk.Button(row, text="Отмена", command=self._cancel,
                  bg=PANEL, fg=FG, relief="flat", width=10).pack(side="left", padx=6)
        tk.Button(row, text="OK", command=self._ok,
                  bg=ACCENT, fg="white", relief="flat", width=10).pack(side="right", padx=6)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())

    def _ok(self):
        self.result = self.entry.get()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# NEW: окно диагностики сети
class NetworkCheckDialog(tk.Toplevel):
    def __init__(self, parent, cfg: Config):
        super().__init__(parent)
        self.cfg = cfg
        self.scanning = False
        self.available_tvs = []   # уникальные {ip, name}

        self.title("Проверка сети")
        self.configure(bg=BG)
        self.geometry("660x640")
        self.minsize(560, 480)
        self.transient(parent)

        # ── Один IP ──────────────────────────────────────────────
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=20, pady=(16, 6))

        tk.Label(top, text="IP:", bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side="left")
        self.ip_var = tk.StringVar()
        e = tk.Entry(top, textvariable=self.ip_var, bg=PANEL, fg=FG,
                     insertbackground=FG, relief="flat", width=22,
                     font=("Consolas", 11))
        e.pack(side="left", padx=(4, 10), ipady=4)
        e.bind("<Return>", lambda ev: self._check_one())

        tk.Label(top, text="Порт:", bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side="left")
        self.port_var = tk.StringVar(value=str(cfg.data.get("port", 8080)))
        tk.Entry(top, textvariable=self.port_var, bg=PANEL, fg=FG,
                 insertbackground=FG, relief="flat", width=6,
                 font=("Consolas", 11)).pack(side="left", padx=(4, 8), ipady=4)

        tk.Button(top, text="Проверить IP", command=self._check_one,
                  bg=ACCENT, fg="white", relief="flat",
                  cursor="hand2").pack(side="left", padx=4)

        # ── Скан всех подсетей ───────────────────────────────────
        scan_row = tk.Frame(self, bg=BG)
        scan_row.pack(fill="x", padx=20, pady=(4, 6))

        subnets = cfg.data.get("subnets") or []
        tk.Label(scan_row,
                 text=f"Подсети из настроек: {', '.join(subnets) if subnets else '—'}.*",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 anchor="w").pack(side="left")

        self.scan_btn = tk.Button(scan_row, text="📡 Сканировать все подсети",
                                   command=self._scan_all,
                                   bg=ACCENT, fg="white", relief="flat",
                                   cursor="hand2")
        self.scan_btn.pack(side="right")

        # ── Статус / прогресс ───────────────────────────────────
        self.status_label = tk.Label(self, text="Готово",
                                      bg=BG, fg=MUTED, anchor="w",
                                      font=("Segoe UI", 10))
        self.status_label.pack(fill="x", padx=22, pady=(4, 2))

        self.progress = ttk.Progressbar(self, mode="determinate",
                                        style="TProgressbar")
        self.progress.pack(fill="x", padx=20, pady=(2, 6))

        # ── Результаты ──────────────────────────────────────────
        result_frame = tk.Frame(self, bg=PANEL)
        result_frame.pack(fill="both", expand=True, padx=20, pady=(4, 10))

        self.result_text = tk.Text(result_frame, bg=BG, fg=FG,
                                    insertbackground=FG,
                                    font=("Consolas", 10),
                                    relief="flat", wrap="word",
                                    bd=0, highlightthickness=0)
        self.result_text.pack(side="left", fill="both", expand=True,
                              padx=8, pady=8)

        scroll = tk.Scrollbar(result_frame, command=self.result_text.yview)
        scroll.pack(side="right", fill="y")
        self.result_text.config(yscrollcommand=scroll.set)

        # ── Кнопки ──────────────────────────────────────────────
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=20, pady=(0, 14))

        tk.Button(bottom, text="📋 Копировать список",
                  command=self._copy_available,
                  bg=PANEL, fg=FG, relief="flat",
                  cursor="hand2").pack(side="left")
        tk.Button(bottom, text="Очистить", command=self._clear,
                  bg=PANEL, fg=FG, relief="flat",
                  cursor="hand2").pack(side="left", padx=6)
        tk.Button(bottom, text="Закрыть", command=self._close,
                  bg=PANEL, fg=FG, relief="flat",
                  cursor="hand2").pack(side="right")

        self._append(
            "Введите IP и нажмите «Проверить IP», либо запустите "
            "сканирование всех подсетей.\n"
            "Сканирование вернёт только те IP, где реально отвечает ТВ.\n\n")

    # ── helpers ─────────────────────────────────────────────────
    def _append(self, text: str):
        self.result_text.insert("end", text)
        self.result_text.see("end")

    def _clear(self):
        self.result_text.delete("1.0", "end")
        self.available_tvs = []
        self.progress["value"] = 0
        self.status_label.config(text="Готово", fg=MUTED)

    def _get_port(self) -> int:
        try:
            return int(self.port_var.get())
        except ValueError:
            return 8080

    def _dedupe_add(self, ip, name):
        if any(t["ip"] == ip for t in self.available_tvs):
            return
        self.available_tvs.append({"ip": ip, "name": name})

    # ── проверка одного IP ──────────────────────────────────────
    def _check_one(self):
        raw = self.ip_var.get().strip()
        if not raw:
            self._append("⚠ Введите IP-адрес\n")
            return
        ip, port = _parse_ip_input(raw, self._get_port())
        if not _is_valid_ip(ip):
            self._append(f"⚠ Неверный формат IP: {ip}\n")
            return

        self.status_label.config(text=f"Проверка {ip}:{port}...", fg=MUTED)
        pin = self.cfg.data.get("pin", "")

        def worker():
            res = check_one(ip, port, pin, timeout=3.0)
            self.after(0, lambda: self._on_check_result(res, port))

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_result(self, res, port):
        ip = res["ip"]
        st = res["status"]
        if st == "ok":
            self._append(f"✅ {ip}:{port} — {res['name']}\n")
            self._dedupe_add(ip, res["name"])
            self.status_label.config(text=f"✅ {ip} доступен", fg=OK_CLR)
        elif st == "pin_required":
            self._append(f"🔒 {ip}:{port} — требует PIN (проверьте настройки)\n")
            self.status_label.config(text=f"🔒 {ip} требует PIN", fg=MUTED)
        elif st == "timeout":
            self._append(f"⏱ {ip}:{port} — таймаут (ТВ выключен / другой IP)\n")
            self.status_label.config(text=f"⏱ {ip} — таймаут", fg=MUTED)
        elif st == "refused":
            self._append(f"❌ {ip}:{port} — соединение отклонено\n")
            self.status_label.config(text=f"❌ {ip} — отказ", fg=ERR_CLR)
        elif st == "invalid_json":
            self._append(f"⚠ {ip}:{port} — ответ не JSON\n")
            self.status_label.config(text=f"⚠ {ip} — неверный ответ", fg=ERR_CLR)
        elif st == "http":
            self._append(f"⚠ {ip}:{port} — HTTP {res.get('code')}\n")
            self.status_label.config(text=f"⚠ {ip} — HTTP {res.get('code')}", fg=ERR_CLR)
        elif st == "not_ok":
            self._append(f"⚠ {ip}:{port} — ответил, но ok=false: {res.get('data')}\n")
            self.status_label.config(text=f"⚠ {ip} — ok=false", fg=ERR_CLR)
        else:
            self._append(f"❌ {ip}:{port} — {res.get('detail', 'ошибка')}\n")
            self.status_label.config(text=f"❌ {ip} — ошибка", fg=ERR_CLR)

    # ── скан всех подсетей ──────────────────────────────────────
    def _scan_all(self):
        if self.scanning:
            return
        subnets = self.cfg.data.get("subnets") or []
        if not subnets:
            self._append("⚠ Нет подсетей в настройках (⚙ Настройки)\n")
            return

        port = self._get_port()
        pin = self.cfg.data.get("pin", "")
        total_ips = len(subnets) * 254

        self.scanning = True
        self.scan_btn.config(state="disabled")
        self.progress["value"] = 0
        self.status_label.config(
            text=f"Сканирование {total_ips} IP...", fg=MUTED)
        self._append(f"\n── Сканирование подсетей: "
                     f"{', '.join(subnets)}.* ──\n")

        def progress_cb(done, total, found):
            self.after(0, self._on_scan_progress, done, total, found)

        def worker():
            try:
                found = scan_network(subnets, port, pin,
                                      progress_cb=progress_cb,
                                      max_workers=40, timeout=1.5)
            except Exception as e:
                self.after(0, lambda: self._on_scan_error(str(e)))
                return
            self.after(0, lambda: self._on_scan_done(found, total_ips))

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_progress(self, done, total, found):
        self.progress["value"] = (done / total * 100) if total else 0
        self.status_label.config(
            text=f"Сканирование: {done} / {total}   найдено ТВ: {found}",
            fg=MUTED)

    def _on_scan_error(self, msg):
        self.scanning = False
        self.scan_btn.config(state="normal")
        self._append(f"❌ Ошибка сканирования: {msg}\n")
        self.status_label.config(text="❌ Ошибка сканирования", fg=ERR_CLR)

    def _on_scan_done(self, found, total):
        self.scanning = False
        self.scan_btn.config(state="normal")
        self.progress["value"] = 100

        if not found:
            self._append(f"❌ Не найдено ни одного ТВ (проверено {total} IP)\n")
            self.status_label.config(text="❌ ТВ не найдены", fg=ERR_CLR)
            return

        self._append(f"\n✅ Найдено ТВ: {len(found)} из {total} IP\n")
        for tv in found:
            self._append(f"   • {tv['name']}  ({tv['ip']})\n")
            self._dedupe_add(tv["ip"], tv["name"])
        self._append("\n")
        self.status_label.config(
            text=f"✅ Найдено ТВ: {len(found)}", fg=OK_CLR)

    # ── копирование / закрытие ──────────────────────────────────
    def _copy_available(self):
        if not self.available_tvs:
            self.status_label.config(text="Нет данных для копирования", fg=MUTED)
            return
        text = "\n".join(f"{t['name']}\t{t['ip']}" for t in self.available_tvs)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status_label.config(
                text=f"📋 Скопировано в буфер: {len(self.available_tvs)} ТВ",
                fg=MUTED)
        except tk.TclError:
            pass

    def _close(self):
        if self.scanning:
            if not messagebox.askyesno(
                "Сканирование идёт",
                "Остановить сканирование и закрыть окно?",
                parent=self,
            ):
                return
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self.title("Настройки")
        self.configure(bg=BG)
        self.geometry("440x520")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        tk.Label(self, text="Подсети (по одной в строке):",
                 bg=BG, fg=FG, anchor="w",
                 font=("Segoe UI", 10)).pack(fill="x", padx=22, pady=(18, 4))
        self.subnets_text = tk.Text(self, height=5, bg=PANEL, fg=FG,
                                    insertbackground=FG, relief="flat",
                                    font=("Consolas", 11))
        self.subnets_text.pack(fill="x", padx=22, ipady=4)
        self.subnets_text.insert("1.0", "\n".join(cfg.data.get("subnets", [])))

        tk.Label(self, text="Порт:", bg=BG, fg=FG, anchor="w",
                 font=("Segoe UI", 10)).pack(fill="x", padx=22, pady=(14, 4))
        self.port_var = tk.StringVar(value=str(cfg.data.get("port", 8080)))
        tk.Entry(self, textvariable=self.port_var, bg=PANEL, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 11)).pack(fill="x", padx=22, ipady=5)

        tk.Label(self, text="PIN (если используется):", bg=BG, fg=FG,
                 anchor="w", font=("Segoe UI", 10)
                 ).pack(fill="x", padx=22, pady=(14, 4))
        self.pin_var = tk.StringVar(value=cfg.data.get("pin", ""))
        tk.Entry(self, textvariable=self.pin_var, show="•", bg=PANEL, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 11)).pack(fill="x", padx=22, ipady=5)

        self.test_label = tk.Label(self, text="", bg=BG, fg=MUTED,
                                   anchor="w", justify="left",
                                   font=("Segoe UI", 9))
        self.test_label.pack(fill="x", padx=22, pady=(12, 0))

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=22, pady=18)
        # FIX: было "Проверить соединение" с фиксированным subnet.1.
        # Теперь открывает полноценное окно диагностики.
        tk.Button(row, text="🔍 Диагностика сети", command=self._test,
                  bg=PANEL, fg=FG, relief="flat",
                  cursor="hand2").pack(side="left")
        tk.Button(row, text="Сохранить", command=self._save,
                  bg=ACCENT, fg="white", relief="flat",
                  width=12, cursor="hand2").pack(side="right")

    def _save(self):
        subnets = [s.strip() for s in
                   self.subnets_text.get("1.0", "end").splitlines()
                   if s.strip()]
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("Ошибка", "Порт должен быть числом", parent=self)
            return
        self.cfg.data["subnets"] = subnets or ["10.62.20"]
        self.cfg.data["port"] = port
        self.cfg.data["pin"] = self.pin_var.get()
        self.cfg.save()
        self.destroy()

    def _test(self):
        # FIX: применяем введённые значения к config (без сохранения на диск),
        # чтобы диагностика работала с актуальными подсетями/портом/PIN.
        subnets = [s.strip() for s in
                   self.subnets_text.get("1.0", "end").splitlines()
                   if s.strip()]
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.test_label.config(text="Неверный порт", fg=ERR_CLR)
            return
        if not subnets:
            self.test_label.config(text="Нет подсетей", fg=ERR_CLR)
            return
        self.cfg.data["subnets"] = subnets
        self.cfg.data["port"] = port
        self.cfg.data["pin"] = self.pin_var.get()
        NetworkCheckDialog(self, self.cfg)


class TvSelectDialog(tk.Toplevel):
    def __init__(self, parent, tvs, on_confirm):
        super().__init__(parent)
        self.on_confirm = on_confirm
        self.title("Найденные телевизоры")
        self.configure(bg=BG)
        self.geometry("500x520")
        self.transient(parent)
        self.grab_set()

        tk.Label(self, text=f"Найдено телевизоров: {len(tvs)}",
                 bg=BG, fg=FG, font=("Segoe UI", 13, "bold")
                 ).pack(pady=(16, 8))

        container = tk.Frame(self, bg=PANEL)
        container.pack(fill="both", expand=True, padx=20, pady=6)

        canvas = tk.Canvas(container, bg=PANEL, highlightthickness=0)
        scroll = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=PANEL)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.vars = []
        for tv in tvs:
            v = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(
                inner, text=f"{tv['name']}   ({tv['ip']})",
                variable=v, bg=PANEL, fg=FG, selectcolor=BG,
                activebackground=PANEL, activeforeground=FG,
                anchor="w", font=("Segoe UI", 10),
            )
            cb.pack(fill="x", padx=10, pady=3)
            self.vars.append((tv, v))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=14)

        tk.Button(btn_row, text="Выбрать все",
                  command=lambda: [v.set(True) for _, v in self.vars],
                  bg=PANEL, fg=FG, relief="flat").pack(side="left")
        tk.Button(btn_row, text="Снять все",
                  command=lambda: [v.set(False) for _, v in self.vars],
                  bg=PANEL, fg=FG, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Отправить", command=self._confirm,
                  bg=ACCENT, fg="white", relief="flat", width=14
                  ).pack(side="right")

    def _confirm(self):
        selected = [tv for tv, v in self.vars if v.get()]
        self.destroy()
        if selected:
            self.on_confirm(selected)


class ResultDialog(tk.Toplevel):
    def __init__(self, parent, results):
        super().__init__(parent)
        self.title("Результаты отправки")
        self.configure(bg=BG)
        self.geometry("540x420")
        self.transient(parent)

        ok = sum(1 for r in results if r.get("ok"))
        fail = len(results) - ok

        tk.Label(self, text=f"Успешно: {ok}   Ошибок: {fail}",
                 bg=BG, fg=FG, font=("Segoe UI", 13, "bold")
                 ).pack(pady=(16, 8))

        text = tk.Text(self, bg=PANEL, fg=FG, relief="flat",
                       font=("Consolas", 10), wrap="word")
        text.pack(fill="both", expand=True, padx=20, pady=6)

        for r in results:
            name = r.get("name") or "?"
            ip = r.get("ip") or "?"
            if r.get("ok"):
                text.insert("end", f"✅ {name} ({ip})\n")
            else:
                text.insert("end", f"❌ {name} ({ip}): {r.get('error', 'ошибка')}\n")
        text.config(state="disabled")

        tk.Button(self, text="Закрыть", command=self.destroy,
                  bg=ACCENT, fg="white", relief="flat", width=14
                  ).pack(pady=12)


# ─── Главное приложение ────────────────────────────────────────────────
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = Config()
        self.logger = setup_logger(self.cfg.dir)

        self.files = {}
        self.busy = False
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.log_lines: list[str] = []

        self._send_files = {}
        self._send_port = 8080
        self._send_pin = ""
        self._send_total = 0

        self._build_ui()
        self._setup_styles()
        self._load_from_config()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._log_pump)

    def _setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TProgressbar",
            troughcolor=PANEL, background=ACCENT,
            bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT,
        )

    def _build_ui(self):
        self.root.title("TV Schedule Uploader")
        self.root.geometry("680x760")
        self.root.minsize(620, 620)
        self.root.configure(bg=BG)

        tk.Label(self.root, text="📺  TV Schedule Uploader",
                 bg=BG, fg=FG, font=("Segoe UI", 18, "bold")
                 ).pack(pady=(18, 6))

        send_frame = tk.Frame(self.root, bg=BG, width=280, height=90)
        send_frame.pack(pady=(6, 14))
        send_frame.pack_propagate(False)

        self.send_btn = tk.Button(
            send_frame, text="📤  ОТПРАВИТЬ\nНА ВСЕ ТВ",
            bg=ACCENT, fg="white", font=("Segoe UI", 14, "bold"),
            relief="flat", activebackground="#6448e0", activeforeground="white",
            cursor="hand2", command=self.on_send, state="disabled",
            disabledforeground="#666",
        )
        self.send_btn.pack(fill="both", expand=True)

        files_panel = tk.Frame(self.root, bg=PANEL)
        files_panel.pack(fill="x", padx=20, pady=6)

        self.html_label = tk.Label(files_panel, text="📄 HTML:       —",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 10),
            anchor="w", justify="left")
        self.html_label.pack(fill="x", padx=14, pady=(10, 2))

        self.panel_label = tk.Label(files_panel, text="📜 Panel:      —",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 10),
            anchor="w", justify="left")
        self.panel_label.pack(fill="x", padx=14, pady=2)

        self.data_label = tk.Label(files_panel, text="📊 Data:       —",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 10),
            anchor="w", justify="left")
        self.data_label.pack(fill="x", padx=14, pady=(2, 10))

        btn_row = tk.Frame(self.root, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(6, 4))

        self._flat_btn(btn_row, "📁 Папка", self.on_pick_folder).pack(side="left")
        self._flat_btn(btn_row, "📄 Файлы", self.on_pick_files).pack(side="left", padx=4)
        self._flat_btn(btn_row, "🔄", self.on_refresh).pack(side="left")
        # NEW: кнопка диагностики
        self._flat_btn(btn_row, "🔍 Проверка сети",
                       self.on_check_network).pack(side="right", padx=4)
        self._flat_btn(btn_row, "⚙", self.on_settings).pack(side="right")

        self.status_label = tk.Label(self.root, text="Готово к работе",
                                     bg=BG, fg=MUTED, anchor="w",
                                     font=("Segoe UI", 10))
        self.status_label.pack(fill="x", padx=22, pady=(10, 2))

        self.progress = ttk.Progressbar(self.root, mode="determinate",
                                        style="TProgressbar")
        self.progress.pack(fill="x", padx=20, pady=(2, 8))

        log_frame = tk.Frame(self.root, bg=PANEL)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(4, 18))

        self.log_text = tk.Text(
            log_frame, bg=BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat", wrap="word",
            state="disabled", bd=0, highlightthickness=0,
        )
        self.log_text.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scroll.set)

    def _flat_btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         bg=PANEL, fg=FG, relief="flat", cursor="hand2",
                         activebackground="#1a2550", activeforeground=FG,
                         font=("Segoe UI", 10))

    def log(self, msg: str, level: str = "INFO"):
        self.logger.log(getattr(logging, level, logging.INFO), msg)
        self.log_queue.put(msg)

    def _log_pump(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_lines.append(msg)
                if len(self.log_lines) > 200:
                    self.log_lines = self.log_lines[-200:]
                self.log_text.config(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.insert("end", "\n".join(self.log_lines))
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._log_pump)

    def _load_from_config(self):
        mode = self.cfg.data.get("mode", "folder")
        if mode == "folder":
            folder = self.cfg.data.get("folder", "")
            if folder and os.path.isdir(folder):
                try:
                    self._apply_folder(folder, silent=True)
                except Exception as e:
                    self.log(f"Не удалось прочитать папку: {e}", "WARNING")
        else:
            html = self.cfg.data.get("html", "")
            panel = self.cfg.data.get("panel", "")
            data = self.cfg.data.get("data", "")
            if html and panel and os.path.isfile(html) and os.path.isfile(panel):
                if not data or not os.path.isfile(data):
                    folder = os.path.dirname(html) or "."
                    data = find_latest_nika(folder) or data
                if data and os.path.isfile(data):
                    self.files = {"html": html, "panel": panel, "data": data}
        self._refresh_files_ui()
        self._validate_ready()

    def on_pick_folder(self):
        initial = self.cfg.data.get("folder") or os.path.expanduser("~")
        folder = filedialog.askdirectory(initialdir=initial,
                                         title="Выберите папку с расписанием")
        if not folder:
            return
        self.cfg.data["mode"] = "folder"
        self.cfg.data["folder"] = folder
        try:
            self._apply_folder(folder)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать папку:\n{e}")
            return
        self.cfg.save()

    def _apply_folder(self, folder, silent=False):
        found = find_files_in_folder(folder)
        missing = [k for k in ("html", "panel", "data") if not found.get(k)]
        if missing and not silent:
            pretty = {"html": "HTML (*.html)",
                      "panel": "schedule_panel.js",
                      "data": "nika_data_*.js"}
            messagebox.showwarning(
                "Файлы не найдены",
                "В папке не найдены:\n" + "\n".join(pretty[m] for m in missing),
            )
        self.files = {k: v for k, v in found.items() if v}
        self.cfg.data["html"] = found.get("html") or ""
        self.cfg.data["panel"] = found.get("panel") or ""
        self.cfg.data["data"] = found.get("data") or ""
        self._refresh_files_ui()
        self._validate_ready()
        if any(found.values()):
            self.log(f"Папка: {folder}")
            if found.get("data"):
                dt = parse_nika_name(os.path.basename(found["data"]))
                if dt:
                    self.log(f"Data: {os.path.basename(found['data'])} "
                             f"({dt:%d.%m.%Y %H:%M:%S})")

    def on_pick_files(self):
        html = filedialog.askopenfilename(
            title="Выберите HTML-файл",
            filetypes=[("HTML", "*.html *.htm"), ("Все файлы", "*.*")])
        if not html:
            return
        panel = filedialog.askopenfilename(
            title="Выберите schedule_panel.js",
            filetypes=[("JS", "*.js"), ("Все файлы", "*.*")])
        if not panel:
            return
        data = filedialog.askopenfilename(
            title="Выберите nika_data_*.js",
            filetypes=[("JS", "*.js"), ("Все файлы", "*.*")])
        if not data:
            return

        if parse_nika_name(os.path.basename(data)) is None:
            if not messagebox.askyesno(
                "Имя не по шаблону",
                "Имя файла данных не соответствует "
                "nika_data_ДДММГГГГ_ЧЧММСС.js.\nВсё равно использовать его?"
            ):
                return

        self.files = {"html": html, "panel": panel, "data": data}
        self.cfg.data["mode"] = "files"
        self.cfg.data["html"] = html
        self.cfg.data["panel"] = panel
        self.cfg.data["data"] = data
        self.cfg.save()
        self._refresh_files_ui()
        self._validate_ready()
        self.log(f"Выбраны файлы: {os.path.basename(html)}, "
                 f"{os.path.basename(panel)}, {os.path.basename(data)}")

    def on_refresh(self):
        if self.cfg.data.get("mode") == "folder":
            folder = self.cfg.data.get("folder", "")
            if folder and os.path.isdir(folder):
                self._apply_folder(folder)
                return
        messagebox.showinfo("Обновление",
                            "Режим «Выбрать файлы». Используйте кнопку "
                            "«Файлы», чтобы указать новые.")

    def on_settings(self):
        SettingsDialog(self.root, self.cfg)

    # NEW: запуск диагностики сети
    def on_check_network(self):
        NetworkCheckDialog(self.root, self.cfg)

    def _refresh_files_ui(self):
        html = self.files.get("html")
        panel = self.files.get("panel")
        data = self.files.get("data")

        if html and os.path.isfile(html):
            try:
                mtime = time.strftime("%d.%m.%Y",
                                      time.localtime(os.path.getmtime(html)))
            except OSError:
                mtime = "?"
            self.html_label.config(
                text=f"📄 HTML:       {os.path.basename(html)}  ({mtime})",
                fg=FG)
        else:
            self.html_label.config(text="📄 HTML:       —", fg=MUTED)

        if panel and os.path.isfile(panel):
            self.panel_label.config(
                text=f"📜 Panel:      {os.path.basename(panel)}  ✅", fg=FG)
        else:
            self.panel_label.config(text="📜 Panel:      —", fg=MUTED)

        if data and os.path.isfile(data):
            dt = parse_nika_name(os.path.basename(data))
            suffix = (f"\n                     "
                      f"({dt:%d.%m.%Y %H:%M:%S})") if dt else ""
            self.data_label.config(
                text=f"📊 Data:       {os.path.basename(data)}{suffix}", fg=FG)
        else:
            self.data_label.config(text="📊 Data:       —", fg=MUTED)

    def _validate_ready(self) -> bool:
        ok = all(self.files.get(k) and os.path.isfile(self.files[k])
                 for k in ("html", "panel", "data"))
        self.send_btn.config(state="normal" if (ok and not self.busy) else "disabled")
        return ok

    def _check_html_matches_data(self):
        try:
            with open(self.files["html"], "rb") as f:
                content = f.read()
            data_name = os.path.basename(self.files["data"]).encode()
            if data_name not in content:
                self.log(
                    f"Внимание: HTML не ссылается на "
                    f"{data_name.decode(errors='replace')}",
                    "WARNING")
        except OSError:
            pass

    def on_send(self):
        if self.busy:
            return
        if not self._validate_ready():
            if messagebox.askyesno(
                "Файлы не выбраны",
                "Сначала выберите три файла расписания.\n"
                "Открыть выбор папки?",
            ):
                self.on_pick_folder()
            return

        self._check_html_matches_data()
        self.busy = True
        self.send_btn.config(state="disabled")
        self.progress["value"] = 0
        self.status_label.config(text="Поиск телевизоров...")
        self.log("Запуск сканирования сети...")

        subnets = self.cfg.data.get("subnets") or []
        port = int(self.cfg.data.get("port", 8080))
        pin = self.cfg.data.get("pin", "")

        threading.Thread(
            target=self._scan_worker,
            args=(subnets, port, pin),
            daemon=True,
        ).start()

    def _scan_worker(self, subnets, port, pin):
        def progress_cb(done, total, found):
            self.root.after(0, self._on_scan_progress, done, total, found)

        try:
            tvs = scan_network(subnets, port, pin, progress_cb,
                               max_workers=40, timeout=1.5)
        except Exception as e:
            self.log(f"Ошибка сканирования: {e}", "ERROR")
            self.root.after(0, self._on_scan_finished, [], True)
            return
        self.root.after(0, self._on_scan_finished, tvs, False)

    def _on_scan_progress(self, done, total, found):
        self.progress["value"] = (done / total * 100) if total else 0
        self.status_label.config(
            text=f"Поиск ТВ... {done} / {total}  (найдено: {found})")

    # FIX: добавлен флаг error; прогресс дорисовывается до 100;
    #      никаких предположений о том, что ТВ есть на каждом IP.
    def _on_scan_finished(self, tvs, had_error=False):
        if had_error:
            self.progress["value"] = 0
        else:
            self.progress["value"] = 100

        if not tvs:
            self.busy = False
            self._validate_ready()
            self.status_label.config(text="Телевизоры не найдены")
            self.log("Телевизоры не найдены", "WARNING")
            messagebox.showwarning(
                "Телевизоры не найдены",
                "Проверьте:\n"
                "• вы в той же Wi-Fi сети?\n"
                "• порт 8080 не блокируется фаерволом?\n"
                "• подсети 10.62.20 / 10.62.21 указаны верно?\n"
                "• PIN не требуется (или указан в настройках)?\n\n"
                "Для точечной диагностики откройте «🔍 Проверка сети».",
            )
            return

        self.log(f"Найдено телевизоров: {len(tvs)}")
        self.status_label.config(
            text=f"Найдено {len(tvs)} телевизоров. Выберите и отправьте.")
        TvSelectDialog(self.root, tvs, self._start_sending)

    def _start_sending(self, tvs):
        self._send_files = {
            "html_path":  self.files["html"],
            "html_name":  os.path.basename(self.files["html"]),
            "panel_path": self.files["panel"],
            "panel_name": "schedule_panel.js",
            "data_path":  self.files["data"],
            "data_name":  os.path.basename(self.files["data"]),
        }
        self._send_port = int(self.cfg.data.get("port", 8080))
        self._send_pin = self.cfg.data.get("pin", "")
        self._send_total = len(tvs)

        self.busy = True
        self.send_btn.config(state="disabled")
        self.progress["value"] = 0
        self.status_label.config(text=f"Отправка: 0 / {self._send_total}")
        self.log(f"Старт отправки на {self._send_total} ТВ...")

        threading.Thread(target=self._send_worker, args=(tvs,),
                         daemon=True).start()

    def _send_worker(self, tvs):
        results = self._run_batch(tvs)

        pin_reqs = [r for r in results if r.get("pin_required")]
        if pin_reqs:
            new_pin = self._ask_pin_blocking()
            if new_pin is None:
                for r in pin_reqs:
                    r["ok"] = False
                    r["error"] = "PIN required (cancelled)"
                    r.pop("pin_required", None)
            else:
                self._send_pin = new_pin
                self.cfg.data["pin"] = new_pin
                self.cfg.save()
                self.log("PIN сохранён, повторная отправка...")
                retry_tvs = [{"ip": r["ip"], "name": r["name"]}
                             for r in pin_reqs]
                retry_results = self._run_batch(retry_tvs)
                results = ([r for r in results if not r.get("pin_required")]
                           + retry_results)

        for r in results:
            if r.get("pin_required"):
                r["ok"] = False
                r["error"] = "PIN неверный или не введён"
                r.pop("pin_required", None)

        self.root.after(0, self._on_send_finished, results)

    def _run_batch(self, tvs):
        results = []
        n = len(tvs)
        done = 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = [
                ex.submit(upload_to_tv, tv, self._send_files,
                          self._send_port, self._send_pin, self.log)
                for tv in tvs
            ]
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"ip": "?", "name": "?", "ok": False,
                           "error": str(e)}
                results.append(res)
                done += 1
                self.root.after(0, self._on_send_progress, done, n, res)
        return results

    def _on_send_progress(self, done, total, result):
        self.progress["value"] = (done / total * 100) if total else 0
        status = "✅" if result.get("ok") else "❌"
        self.status_label.config(
            text=f"Отправка: {done} / {total}   {status} "
                 f"{result.get('name', '')} ({result.get('ip', '')})")

    def _on_send_finished(self, results):
        ok = sum(1 for r in results if r.get("ok"))
        fail = len(results) - ok

        self.busy = False
        self._validate_ready()
        self.progress["value"] = 100
        self.status_label.config(
            text=f"Готово. Успешно: {ok}, ошибок: {fail}")
        self.log(f"Итог отправки: успешно {ok}, ошибок {fail}",
                 "INFO" if fail == 0 else "WARNING")

        ResultDialog(self.root, results)

    def _ask_pin_blocking(self):
        event = threading.Event()
        box = {"pin": None}

        def show():
            dlg = PinDialog(self.root)
            self.root.wait_window(dlg)
            box["pin"] = dlg.result
            event.set()

        self.root.after(0, show)
        event.wait()
        return box["pin"]

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno(
                "Подтверждение",
                "Идёт отправка/сканирование. "
                "Выйти, не дожидаясь окончания?",
            ):
                return
        try:
            self.cfg.save()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    app.log("Приложение запущено")
    root.mainloop()


if __name__ == "__main__":
    main()