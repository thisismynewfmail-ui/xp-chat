"""Chat session persistence + server-sent event bus.

Chats live in data/chats/<id>.json. All mutation goes through the Store so
every running view can be kept in sync via the EventBus.
"""

import json
import os
import queue
import threading
import time
import uuid


def now_ms():
    return int(time.time() * 1000)


def new_id():
    return uuid.uuid4().hex[:12]


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class Store:
    def __init__(self, chats_dir, bus):
        self.dir = chats_dir
        self.bus = bus
        self._lock = threading.RLock()
        self._cache = {}
        os.makedirs(self.dir, exist_ok=True)

    # ---- helpers ---------------------------------------------------------
    def _path(self, cid):
        safe = "".join(ch for ch in cid if ch.isalnum())
        return os.path.join(self.dir, f"{safe}.json")

    def _write(self, chat):
        path = self._path(chat["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(chat, f, ensure_ascii=False)
        os.replace(tmp, path)

    # ---- CRUD ------------------------------------------------------------
    def list_chats(self):
        with self._lock:
            out = []
            for name in os.listdir(self.dir):
                if not name.endswith(".json"):
                    continue
                chat = self.load(name[:-5])
                if not chat:
                    continue
                last = ""
                for m in reversed(chat["messages"]):
                    if m.get("content"):
                        last = m["content"][:80]
                        break
                out.append({
                    "id": chat["id"],
                    "name": chat.get("name") or "untitled",
                    "created": chat.get("created", 0),
                    "updated": chat.get("updated", 0),
                    "count": len(chat["messages"]),
                    "preview": last,
                })
            out.sort(key=lambda c: c["updated"], reverse=True)
            return out

    def load(self, cid):
        with self._lock:
            if cid in self._cache:
                return self._cache[cid]
            path = self._path(cid)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    chat = json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
            chat.setdefault("messages", [])
            self._cache[cid] = chat
            return chat

    def save(self, chat, quiet=False):
        with self._lock:
            chat["updated"] = now_ms()
            self._cache[chat["id"]] = chat
            self._write(chat)
        if not quiet:
            self.bus.publish({"type": "chats"})

    def create(self, name=None):
        with self._lock:
            n = len(self.list_chats()) + 1
            chat = {
                "id": new_id(),
                "name": name or f"session {n:03d}",
                "created": now_ms(),
                "updated": now_ms(),
                "messages": [],
                "cut_mid": None,     # first in-context message id (context divider)
            }
            self.save(chat)
            return chat

    def rename(self, cid, name):
        chat = self.load(cid)
        if not chat:
            return None
        chat["name"] = (name or "").strip() or chat["name"]
        self.save(chat)
        return chat

    def duplicate(self, cid):
        src = self.load(cid)
        if not src:
            return None
        dup = json.loads(json.dumps(src))
        dup["id"] = new_id()
        dup["name"] = f"{src.get('name', 'session')} (copy)"
        dup["created"] = now_ms()
        self.save(dup)
        return dup

    def delete(self, cid):
        with self._lock:
            self._cache.pop(cid, None)
            path = self._path(cid)
            if os.path.exists(path):
                os.remove(path)
        self.bus.publish({"type": "chats"})
        return True

    def clear_messages(self, cid):
        chat = self.load(cid)
        if not chat:
            return None
        chat["messages"] = []
        chat["cut_mid"] = None
        self.save(chat)
        self.bus.publish({"type": "chat", "chat": cid})
        return chat
