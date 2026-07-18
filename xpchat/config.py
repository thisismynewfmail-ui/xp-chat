"""Settings management.

Precedence (lowest to highest):
  1. built-in DEFAULT_SETTINGS
  2. the bundled seed config file (`.config` or `config.json`) in the app dir
  3. data/settings.json — everything saved from the UI settings menu

Settings saved from the UI always win and are persisted, so every running
view stays in sync (the server broadcasts a `settings` event on save).
"""

import copy
import json
import os
import threading

DEFAULT_SETTINGS = {
    "endpoint": "http://127.0.0.1:8080",
    "api_key": "",
    "model": "",                      # empty = first model reported by /v1/models
    "bot_name": "Tachikoma",
    "user_name": "User",
    "system_prompt": (
        "You are Tachikoma, a cheerful and endlessly curious AI think-tank "
        "serving Public Security Section 9. Answer helpfully and precisely."
    ),
    "context_length": 8196,           # tokens; spec default
    "context_fill_ratio": 0.75,       # trim conversation once prompt exceeds this share
    "stream": True,
    "thinking": {
        "enabled": True,              # ask the model to think (enable_thinking)
        "display": "collapsed",       # collapsed | expanded | hidden
    },
    "send_enable_thinking": True,     # pass chat_template_kwargs.enable_thinking upstream
    "template_mode": "auto",          # auto (use server template) | preset | custom
    "template_preset": "ChatML (Qwen)",
    "template_custom": "",
    "sampling": {
        # Keys are named EXACTLY as the OpenAI-compatible endpoint expects them
        # and are passed verbatim in the /v1/chat/completions payload.
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.05,
        "typical_p": 1.0,
        "repeat_penalty": 1.1,
        "repeat_last_n": 64,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "max_tokens": 1024,
        "seed": -1,                   # -1 = random (omitted from the payload)
    },
    "ui": {
        "scanlines": True,
        "net_monitor": True,
        "animations": True,
    },
}

# seed-config key -> settings key (identity unless noted)
SEED_KEYS = ("endpoint", "model", "context_length", "system_prompt", "stream",
             "api_key", "bot_name", "user_name", "context_fill_ratio")


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class AppConfig:
    def __init__(self, base_dir, data_dir):
        self.base_dir = base_dir
        self.data_dir = data_dir
        self.chats_dir = os.path.join(data_dir, "chats")
        os.makedirs(self.chats_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._settings_path = os.path.join(data_dir, "settings.json")
        self.settings = self._load()

    # ---- seed config -----------------------------------------------------
    def _seed_config(self):
        for name in (".config", "config.json"):
            path = os.path.join(self.base_dir, name)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                seed = {k: raw[k] for k in SEED_KEYS if raw.get(k) is not None}
                if isinstance(raw.get("sampling"), dict):
                    seed["sampling"] = {k: v for k, v in raw["sampling"].items()
                                        if v is not None}
                return seed
        return {}

    # ---- persistence -----------------------------------------------------
    def _load(self):
        merged = deep_merge(DEFAULT_SETTINGS, self._seed_config())
        if os.path.exists(self._settings_path):
            try:
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    merged = deep_merge(merged, json.load(f))
            except (OSError, json.JSONDecodeError):
                pass
        return merged

    def save(self):
        with self._lock:
            tmp = self._settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._settings_path)

    def update(self, patch):
        """Deep-merge a settings patch, sanitize, persist. Returns settings."""
        with self._lock:
            self.settings = deep_merge(self.settings, patch or {})
            self._sanitize()
            self.save()
            return self.settings

    def _sanitize(self):
        s = self.settings
        try:
            s["context_length"] = max(512, int(s.get("context_length") or 8196))
        except (TypeError, ValueError):
            s["context_length"] = 8196
        try:
            fr = float(s.get("context_fill_ratio") or 0.75)
        except (TypeError, ValueError):
            fr = 0.75
        s["context_fill_ratio"] = min(0.98, max(0.30, fr))
        if s.get("thinking", {}).get("display") not in ("collapsed", "expanded", "hidden"):
            s.setdefault("thinking", {})["display"] = "collapsed"
        if s.get("template_mode") not in ("auto", "preset", "custom"):
            s["template_mode"] = "auto"
        if not (s.get("user_name") or "").strip():
            s["user_name"] = "User"
        if not (s.get("bot_name") or "").strip():
            s["bot_name"] = "Tachikoma"
