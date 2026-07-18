"""Upstream client for the OpenAI-compatible endpoint.

Standard library only (urllib). Talks directly to the endpoint, bypassing any
system proxy — LLM endpoints are normally on the local network.

Sampling keys are passed in the /v1/chat/completions payload with EXACTLY
these names: temperature, top_p, top_k, min_p, typical_p, repeat_penalty,
repeat_last_n, presence_penalty, frequency_penalty, max_tokens, seed.
"""

import json
import time
import urllib.error
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FLOAT_KEYS = ("temperature", "top_p", "min_p", "typical_p", "repeat_penalty",
              "presence_penalty", "frequency_penalty")
INT_KEYS = ("top_k", "repeat_last_n", "max_tokens", "seed")

# /props default_generation_settings.params -> our sampling keys
PROPS_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "typical_p": "typical_p",
    "typ_p": "typical_p",
    "repeat_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "max_tokens": "max_tokens",
    "n_predict": "max_tokens",
    "seed": "seed",
}


class UpstreamError(Exception):
    pass


def split_endpoint(endpoint):
    """Return (root, v1) base URLs from a configured endpoint string."""
    ep = (endpoint or "").strip().rstrip("/")
    if ep.endswith("/v1"):
        return ep[:-3], ep
    return ep, ep + "/v1"


def _headers(api_key):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _request(url, api_key, payload=None, timeout=10, stream=False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(api_key),
                                 method="POST" if data is not None else "GET")
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        raise UpstreamError(f"HTTP {e.code} from {url}: {body[:300]}")
    except (urllib.error.URLError, OSError) as e:
        raise UpstreamError(f"cannot reach {url}: {getattr(e, 'reason', e)}")


def get_json(url, api_key, timeout=8):
    with _request(url, api_key, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(url, api_key, payload, timeout=15):
    with _request(url, api_key, payload=payload, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_models(endpoint, api_key):
    _, v1 = split_endpoint(endpoint)
    data = get_json(f"{v1}/models", api_key)
    return [m.get("id", "?") for m in (data.get("data") or [])]


def fetch_props(endpoint, api_key):
    """llama.cpp /props: chat template, context size, default sampling.
    Returns {} for servers that don't expose it."""
    root, _ = split_endpoint(endpoint)
    try:
        return get_json(f"{root}/props", api_key) or {}
    except UpstreamError:
        return {}


def props_sampling(props):
    """Map /props default_generation_settings into our sampling key names."""
    params = (props.get("default_generation_settings") or {}).get("params") or {}
    out = {}
    for src, dst in PROPS_MAP.items():
        if src in params and params[src] is not None:
            val = params[src]
            if dst == "max_tokens" and (val is None or val < 0):
                continue
            if dst == "seed" and val in (4294967295, 4294967294):
                val = -1
            out[dst] = val
    return out


def props_n_ctx(props):
    return ((props.get("default_generation_settings") or {}).get("n_ctx")
            or props.get("n_ctx") or 0)


def props_template(props):
    return props.get("chat_template") or ""


def tokenize_count(endpoint, api_key, text):
    """Exact token count via llama.cpp /tokenize; None if unsupported."""
    if not text:
        return 0
    root, _ = split_endpoint(endpoint)
    try:
        data = post_json(f"{root}/tokenize", api_key, {"content": text}, timeout=8)
        toks = data.get("tokens")
        if isinstance(toks, list):
            return len(toks)
    except UpstreamError:
        pass
    return None


def estimate_tokens(text):
    return max(1, (len(text) + 3) // 4) if text else 0


def build_sampling(sampling):
    """Sanitize sampling settings into correctly-typed payload fields."""
    out = {}
    for k in FLOAT_KEYS:
        v = sampling.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    for k in INT_KEYS:
        v = sampling.get(k)
        if v is None:
            continue
        try:
            iv = int(float(v))
        except (TypeError, ValueError):
            continue
        if k == "seed" and iv < 0:
            continue          # -1 = random -> omit so the server rolls its own
        if k == "max_tokens" and iv <= 0:
            continue
        out[k] = iv
    return out


def chat_stream(endpoint, api_key, payload, on_event, stop_check=None):
    """POST /v1/chat/completions. Streams SSE chunks, calling
    on_event(delta_dict) per chunk. Returns the finish reason.
    Falls back to non-streaming when payload["stream"] is false."""
    _, v1 = split_endpoint(endpoint)
    url = f"{v1}/chat/completions"

    if not payload.get("stream"):
        obj = post_json(url, api_key, payload, timeout=600)
        msg = (obj.get("choices") or [{}])[0].get("message") or {}
        if msg.get("reasoning_content"):
            on_event({"reasoning_content": msg["reasoning_content"]})
        if msg.get("content"):
            on_event({"content": msg["content"]})
        return (obj.get("choices") or [{}])[0].get("finish_reason") or "stop"

    finish = "stop"
    resp = _request(url, api_key, payload=payload, timeout=600, stream=True)
    try:
        for raw in resp:
            if stop_check and stop_check():
                finish = "aborted"
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = (obj.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta:
                on_event(delta)
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return finish


class ThinkSplitter:
    """Incrementally split streamed content into (visible, thinking) parts,
    handling <think>...</think> tags even when a tag is split across chunks."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self.pend = ""

    def _hold(self, s):
        # keep a possible partial tag at the end of the buffer for next chunk
        for i in range(len(s) - 1, max(-1, len(s) - len(self.CLOSE)), -1):
            if s[i] == "<":
                tail = s[i:]
                if self.OPEN.startswith(tail) or self.CLOSE.startswith(tail):
                    return s[:i], s[i:]
        return s, ""

    def feed(self, piece):
        out_c, out_t = [], []
        buf = self.pend + (piece or "")
        self.pend = ""
        while buf:
            tag = self.CLOSE if self.in_think else self.OPEN
            idx = buf.find(tag)
            if idx >= 0:
                (out_t if self.in_think else out_c).append(buf[:idx])
                buf = buf[idx + len(tag):]
                self.in_think = not self.in_think
            else:
                emit, hold = self._hold(buf)
                (out_t if self.in_think else out_c).append(emit)
                self.pend = hold
                buf = ""
        return "".join(out_c), "".join(out_t)

    def flush(self):
        c, t = ("", self.pend) if self.in_think else (self.pend, "")
        self.pend = ""
        return c, t


def ping(endpoint, api_key):
    """Quick reachability probe. Returns (ok, latency_ms, detail)."""
    t0 = time.time()
    try:
        models = fetch_models(endpoint, api_key)
        return True, int((time.time() - t0) * 1000), models
    except UpstreamError as e:
        return False, int((time.time() - t0) * 1000), str(e)
