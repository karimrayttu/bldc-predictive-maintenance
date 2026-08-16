"""Live log: a small file-backed observability sink, so you can see what the app is
doing while it runs by reading plain files. No live IPC needed.
"""

import os
import json
import datetime


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class LiveLog:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "_live")
        os.makedirs(self.dir, exist_ok=True)
        self.status_path = os.path.join(self.dir, "status.json")
        self.events_path = os.path.join(self.dir, "events.log")
        self.event("app started")

    def event(self, text):
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(f"{_now()}  {text}\n")
        except Exception:
            pass

    def status(self, data):
        try:
            data = dict(data); data["updated"] = _now()
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.status_path)
        except Exception:
            pass
