"""HTTP server: static UI, JSON API, SSE sync channel, generation manager.

Generation runs server-side so that every connected view (any number of tabs
or machines pointed at this server) sees the same live-streaming state.
"""

import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import llm
from .contextman import trim_messages
from .store import Store, EventBus, new_id, now_ms
from .templates import PRESETS, resolve_template

STATIC_DIR = None  # set in create_server


# --------------------------------------------------------------------------
# Endpoint status monitor
# --------------------------------------------------------------------------
class EndpointMonitor:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.lock = threading.Lock()
        self.status = {"online": False, "latency_ms": None, "models": [],
                       "n_ctx": 0, "chat_template": "", "server_sampling": {},
                       "endpoint": "", "checked": 0, "error": ""}
        self._wake = threading.Event()
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while True:
            self.check()
            self._wake.wait(timeout=10)
            self._wake.clear()

    def poke(self):
        self._wake.set()

    def check(self, force_props=False):
        s = self.cfg.settings
        ep, key = s.get("endpoint", ""), s.get("api_key", "")
        ok, latency, detail = llm.ping(ep, key)
        with self.lock:
            was_online = self.status["online"]
            changed_ep = self.status["endpoint"] != ep
            st = dict(self.status)
        st.update({"online": ok, "latency_ms": latency, "endpoint": ep,
                   "checked": now_ms(), "error": "" if ok else str(detail)})
        if ok:
            st["models"] = detail
            if force_props or not was_online or changed_ep:
                props = llm.fetch_props(ep, key)
                st["n_ctx"] = llm.props_n_ctx(props)
                st["chat_template"] = llm.props_template(props)
                st["server_sampling"] = llm.props_sampling(props)
        with self.lock:
            notify = (st["online"] != self.status["online"]
                      or st["models"] != self.status["models"]
                      or changed_ep)
            self.status = st
        if notify:
            self.bus.publish({"type": "status", "status": st})
        return st

    def get(self):
        with self.lock:
            return dict(self.status)


# --------------------------------------------------------------------------
# Token counting (exact via /tokenize when available, estimate otherwise)
# --------------------------------------------------------------------------
class TokenCounter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}
        self.exact = True

    def count(self, text):
        if not text:
            return 0
        key = hash(text)
        if key in self.cache:
            return self.cache[key]
        n = None
        if self.exact:
            s = self.cfg.settings
            n = llm.tokenize_count(s.get("endpoint", ""), s.get("api_key", ""), text)
            if n is None:
                self.exact = False   # endpoint has no /tokenize; stop asking
        if n is None:
            n = llm.estimate_tokens(text)
        if len(self.cache) > 4096:
            self.cache.clear()
        self.cache[key] = n
        return n


# --------------------------------------------------------------------------
# Generation manager — one generation per chat, streamed to every client
# --------------------------------------------------------------------------
class GenManager:
    def __init__(self, cfg, store, bus, monitor, counter):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.monitor = monitor
        self.counter = counter
        self.lock = threading.Lock()
        self.active = {}   # chat_id -> {"stop": Event, "mid": str}

    def is_busy(self, cid):
        with self.lock:
            return cid in self.active

    def stop(self, cid):
        with self.lock:
            task = self.active.get(cid)
        if task:
            task["stop"].set()
            return True
        return False

    def start(self, cid, user_text=None, regenerate=False):
        chat = self.store.load(cid)
        if not chat:
            return None, "no such chat"
        with self.lock:
            if cid in self.active:
                return None, "generation already running for this chat"
            stop = threading.Event()
            self.active[cid] = {"stop": stop, "mid": None}

        s = self.cfg.settings
        if regenerate:
            while chat["messages"] and chat["messages"][-1]["role"] == "assistant":
                chat["messages"].pop()
        if user_text is not None:
            chat["messages"].append({
                "id": new_id(), "role": "user", "name": s.get("user_name", "User"),
                "content": user_text, "ts": now_ms(),
            })
        self.store.save(chat)
        self.bus.publish({"type": "chat", "chat": cid})

        t = threading.Thread(target=self._run, args=(chat, stop), daemon=True)
        t.start()
        return True, None

    # ---- payload assembly ------------------------------------------------
    def build_messages(self, chat, settings):
        """History as sent upstream: system message + trimmed conversation.
        Thinking is never replayed to the model."""
        msgs = []
        sysp = (settings.get("system_prompt") or "").strip()
        if sysp:
            msgs.append({"role": "system", "content": sysp})
        uname = settings.get("user_name") or "User"
        for m in chat["messages"]:
            if m["role"] == "user":
                msgs.append({"role": "user", "name": uname,
                             "content": m.get("content") or "", "_mid": m["id"]})
            elif m["role"] == "assistant" and (m.get("content") or "").strip():
                msgs.append({"role": "assistant", "content": m["content"],
                             "_mid": m["id"]})
        kept, dropped, used, budget = trim_messages(msgs, self.counter.count,
                                                    settings)
        # remember where the in-context window now starts, for the UI divider
        cut_mid = None
        if dropped:
            first_kept = next((m for m in kept if m["role"] != "system"), None)
            if first_kept is not None:
                cut_mid = first_kept.get("_mid")
        chat["cut_mid"] = cut_mid
        kept = [{k: v for k, v in m.items() if k != "_mid"} for m in kept]
        return kept, dropped, used, budget

    def build_payload(self, settings, messages, status):
        payload = {"messages": messages, "stream": bool(settings.get("stream", True))}
        model = settings.get("model") or (status.get("models") or [""])[0]
        if model:
            payload["model"] = model
        payload.update(llm.build_sampling(settings.get("sampling") or {}))
        tpl = resolve_template(settings, status.get("chat_template"))
        if tpl:
            payload["chat_template"] = tpl
        if settings.get("send_enable_thinking", True):
            payload["chat_template_kwargs"] = {
                "enable_thinking": bool((settings.get("thinking") or {}).get("enabled", True))
            }
        return payload

    # ---- the generation thread ------------------------------------------
    def _run(self, chat, stop):
        cid = chat["id"]
        s = self.cfg.settings
        status = self.monitor.get()
        mid = new_id()
        msg = {"id": mid, "role": "assistant", "name": s.get("bot_name", "Tachikoma"),
               "content": "", "thinking": "", "ts": now_ms(), "streaming": True}
        try:
            kept, dropped, used, budget = self.build_messages(chat, s)
            payload = self.build_payload(s, kept, status)

            chat["messages"].append(msg)
            self.store.save(chat, quiet=True)
            with self.lock:
                if cid in self.active:
                    self.active[cid]["mid"] = mid
            self.bus.publish({"type": "gen_start", "chat": cid, "mid": mid,
                              "name": msg["name"],
                              "ctx": {"used": used, "budget": budget,
                                      "limit": s.get("context_length"),
                                      "dropped": dropped,
                                      "cut_mid": chat.get("cut_mid")}})
            if dropped:
                self.bus.publish({"type": "log",
                                  "text": f"context crop: {dropped} message(s) fell out of the window"})

            splitter = llm.ThinkSplitter()
            state = {"last_save": time.time(), "tok": 0, "t0": time.time()}

            def push(field, text):
                if not text:
                    return
                if not msg[field]:
                    text = text.lstrip("\n")
                    if not text:
                        return
                msg[field] += text
                self.bus.publish({"type": "delta", "chat": cid, "mid": mid,
                                  "field": field, "text": text})

            def on_event(delta):
                state["tok"] += 1
                rc = delta.get("reasoning_content")
                if rc:
                    push("thinking", rc)
                c = delta.get("content")
                if c:
                    vis, think = splitter.feed(c)
                    push("content", vis)
                    push("thinking", think)
                if time.time() - state["last_save"] > 2.0:
                    state["last_save"] = time.time()
                    self.store.save(chat, quiet=True)

            finish = llm.chat_stream(s.get("endpoint", ""), s.get("api_key", ""),
                                     payload, on_event, stop_check=stop.is_set)
            vis, think = splitter.flush()
            push("content", vis)
            push("thinking", think)
            if stop.is_set():
                finish = "aborted"
            msg["content"] = msg["content"].rstrip()
            secs = max(0.001, time.time() - state["t0"])
            msg["stats"] = {"finish": finish, "tokens": state["tok"],
                            "tps": round(state["tok"] / secs, 1), "secs": round(secs, 1)}
        except llm.UpstreamError as e:
            msg["error"] = str(e)
            if msg not in chat["messages"]:
                chat["messages"].append(msg)
            self.bus.publish({"type": "log", "text": f"uplink error: {e}"})
        finally:
            msg.pop("streaming", None)
            self.store.save(chat)
            with self.lock:
                self.active.pop(cid, None)
            self.bus.publish({"type": "gen_end", "chat": cid, "mid": mid,
                              "stats": msg.get("stats"), "error": msg.get("error")})


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bus = EventBus()
        self.store = Store(cfg.chats_dir, self.bus)
        self.monitor = EndpointMonitor(cfg, self.bus)
        self.counter = TokenCounter(cfg)
        self.gen = GenManager(cfg, self.store, self.bus, self.monitor, self.counter)


class Handler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    # ---- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _file(self, relpath):
        path = os.path.normpath(os.path.join(STATIC_DIR, relpath))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- SSE -------------------------------------------------------------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.app.bus.subscribe()
        try:
            hello = {"type": "hello", "status": self.app.monitor.get()}
            self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                except Exception as e:
                    if isinstance(e, (BrokenPipeError, ConnectionError, OSError)):
                        raise
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(q)

    # ---- routing ---------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path.startswith("/static/"):
            return self._file(path[len("/static/"):])
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/events":
            return self._sse()
        if path == "/api/state":
            return self._json({
                "settings": self.app.cfg.settings,
                "chats": self.app.store.list_chats(),
                "status": self.app.monitor.get(),
                "presets": list(PRESETS.keys()),
            })
        if path == "/api/presets":
            return self._json(PRESETS)
        m = re.match(r"^/api/chats/([a-f0-9]+)/export$", path)
        if m:
            return self._export(m.group(1))
        m = re.match(r"^/api/chats/([a-f0-9]+)$", path)
        if m:
            chat = self.app.store.load(m.group(1))
            if not chat:
                return self._json({"error": "no such chat"}, 404)
            used, budget = self._ctx_estimate(chat)
            return self._json({"chat": chat,
                               "busy": self.app.gen.is_busy(chat["id"]),
                               "ctx": {"used": used, "budget": budget,
                                       "limit": self.app.cfg.settings.get("context_length"),
                                       "cut_mid": chat.get("cut_mid")}})
        if path == "/api/chats":
            return self._json({"chats": self.app.store.list_chats()})
        self.send_error(404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/settings":
            body = self._body()
            old_ep = self.app.cfg.settings.get("endpoint")
            settings = self.app.cfg.update(body)
            if settings.get("endpoint") != old_ep:
                self.app.counter.exact = True
                self.app.counter.cache.clear()
                self.app.monitor.poke()
            self.app.bus.publish({"type": "settings", "settings": settings})
            return self._json({"ok": True, "settings": settings})
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        app = self.app
        if path == "/api/test":
            st = app.monitor.check(force_props=True)
            return self._json({"ok": st["online"], "status": st})
        if path == "/api/chats":
            body = self._body()
            chat = app.store.create(body.get("name"))
            return self._json({"ok": True, "chat": chat})

        m = re.match(r"^/api/chats/([a-f0-9]+)(?:/(\w+))?$", path)
        if not m:
            return self.send_error(404)
        cid, action = m.group(1), m.group(2)

        if action == "send":
            text = (self._body().get("text") or "").strip()
            if not text:
                return self._json({"error": "empty message"}, 400)
            ok, err = app.gen.start(cid, user_text=text)
            return self._json({"ok": bool(ok), "error": err},
                              200 if ok else 409)
        if action == "regen":
            ok, err = app.gen.start(cid, regenerate=True)
            return self._json({"ok": bool(ok), "error": err},
                              200 if ok else 409)
        if action == "stop":
            return self._json({"ok": app.gen.stop(cid)})
        if action == "rename":
            chat = app.store.rename(cid, self._body().get("name"))
            return self._json({"ok": bool(chat)})
        if action == "duplicate":
            chat = app.store.duplicate(cid)
            return self._json({"ok": bool(chat),
                               "chat": chat} if chat else {"ok": False})
        if action == "clear":
            chat = app.store.clear_messages(cid)
            return self._json({"ok": bool(chat)})
        self.send_error(404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/chats/([a-f0-9]+)$", path)
        if m:
            if self.app.gen.is_busy(m.group(1)):
                self.app.gen.stop(m.group(1))
            self.app.store.delete(m.group(1))
            return self._json({"ok": True})
        self.send_error(404)

    # ---- misc ------------------------------------------------------------
    def _ctx_estimate(self, chat):
        s = self.app.cfg.settings
        msgs = []
        if (s.get("system_prompt") or "").strip():
            msgs.append({"role": "system", "content": s["system_prompt"]})
        msgs += [{"role": m["role"], "content": m.get("content") or ""}
                 for m in chat["messages"]]
        _, _, used, budget = trim_messages(msgs, self.app.counter.count, s)
        return used, budget

    def _export(self, cid):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        fmt = (qs.get("fmt") or ["json"])[0]
        chat = self.app.store.load(cid)
        if not chat:
            return self._json({"error": "no such chat"}, 404)
        name = re.sub(r"[^\w\- ]", "_", chat.get("name") or "chat")
        if fmt == "txt":
            lines = [f"# {chat.get('name')}", ""]
            for m in chat["messages"]:
                who = m.get("name") or m["role"]
                lines.append(f"[{who}]")
                if m.get("thinking"):
                    lines.append("(thinking) " + m["thinking"].strip())
                lines.append(m.get("content") or "")
                lines.append("")
            body = "\n".join(lines).encode("utf-8")
            ctype, ext = "text/plain; charset=utf-8", "txt"
        else:
            body = json.dumps(chat, indent=2, ensure_ascii=False).encode("utf-8")
            ctype, ext = "application/json; charset=utf-8", "json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{name}.{ext}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host, port, cfg):
    global STATIC_DIR
    STATIC_DIR = os.path.join(cfg.base_dir, "static")
    app = App(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
