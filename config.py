"""Загрузка/сохранение config.json в %APPDATA%/TV Schedule Uploader/"""
import os
import json


APP_NAME = "TV Schedule Uploader"

DEFAULTS = {
    "mode": "folder",
    "folder": "",
    "html": "",
    "panel": "",
    "data": "",
    "subnets": ["10.62.20", "10.62.21"],
    "port": 8080,
    "pin": "",
}


class Config:
    def __init__(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        self.dir = os.path.join(base, APP_NAME)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "config.json")
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k in self.data:
                    self.data[k] = v
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass