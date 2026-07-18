#!/usr/bin/env python3
"""Terminal chat client for an OpenAI-compatible endpoint (llama.cpp et al.).

On startup it pulls the model list from /v1/models and the server settings
(chat template, default sampling, context size, capabilities) from /props,
then uses /v1/chat/completions with the server applying the exact chat
template we fetched.
"""

import argparse
import json
import sys
from pathlib import Path

import requests
import jinja2

CONFIG_PATH = Path(__file__).with_name("config.json")

SAMPLING_KEYS = [
    "temperature", "top_p", "top_k", "min_p", "repeat_penalty",
    "repeat_last_n", "presence_penalty", "frequency_penalty",
    "max_tokens", "seed",
]

# /props default_generation_settings -> our sampling keys
PROPS_TO_SAMPLING = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "repeat_penalty": "repeat_penalty",
    "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "max_tokens": "max_tokens",
    "seed": "seed",
}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"could not parse {path}: {e}")


def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_args(config):
    p = argparse.ArgumentParser(description="Terminal chat for an OpenAI-compatible endpoint.")
    p.add_argument("--config", type=Path, default=CONFIG_PATH)
    p.add_argument("--endpoint", type=str, default=None)
    p.add_argument("--model", type=str, default=None)
    p.add_argument("--context-length", type=int, default=None, dest="context_length")
    p.add_argument("--system", type=str, default=None)
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--render", action="store_true",
                   help="render the current (empty) conversation with the server template and exit")
    for key in SAMPLING_KEYS:
        p.add_argument(f"--{key.replace('_', '-')}", type=float, default=None, dest=key)
    a = p.parse_args()
    cli = {k: getattr(a, k) for k in SAMPLING_KEYS}
    cli_overlay = {
        "endpoint": a.endpoint,
        "model": a.model,
        "context_length": a.context_length,
        "system_prompt": a.system,
        "stream": False if a.no_stream else None,
    }
    cli_overlay = {k: v for k, v in cli_overlay.items() if v is not None}
    cli_overlay["sampling"] = {k: v for k, v in cli.items() if v is not None}
    return a, deep_merge(config, cli_overlay)


def get_json(url, timeout=10):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        die(f"GET {url} failed: {e}")


def pick_model(v1_base, configured):
    data = get_json(f"{v1_base}/models")
    models = data.get("data") or []
    if not models:
        die("no models reported by /v1/models")
    if configured:
        return configured
    return models[0]["id"]


def fetch_props(root_base):
    return get_json(f"{root_base}/props")


def normalize_props_sampling(props):
    gs = props.get("default_generation_settings", {}).get("params", {})
    out = {}
    for src, dst in PROPS_TO_SAMPLING.items():
        if src in gs:
            val = gs[src]
            if dst == "max_tokens" and val == -1:
                val = 2048
            if dst == "seed" and val in (4294967295, 4294967295 - 1):
                val = -1
            out[dst] = val
    return out


def make_token_counter(root_base):
    def count(text):
        if not text:
            return 0
        try:
            r = requests.post(f"{root_base}/tokenize", json={"content": text}, timeout=10)
            if r.ok:
                toks = r.json().get("tokens")
                if isinstance(toks, list):
                    return len(toks)
        except requests.RequestException:
            pass
        return max(1, len(text) // 4)

    return count


def build_jinja_env():
    env = jinja2.Environment(keep_trailing_newline=True)
    env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(Exception(msg))
    return env


def render_preview(template_str, messages):
    env = build_jinja_env()
    try:
        tmpl = env.from_string(template_str)
        return tmpl.render(messages=messages, add_generation_prompt=True,
                           enable_thinking=None, preserve_thinking=False)
    except Exception as e:
        return f"<template render error: {e}>"


def trim_messages(messages, budget, count):
    sys_msgs = [m for m in messages if m["role"] == "system"]
    others = [m for m in messages if m["role"] != "system"]
    sys_used = sum(count(m.get("content", "")) for m in sys_msgs)
    kept = []
    used = sys_used
    for m in reversed(others):
        c = count(m.get("content", ""))
        if kept and used + c > budget:
            break
        kept.append(m)
        used += c
    if not kept and others:
        kept = [others[-1]]
    kept.reverse()
    return sys_msgs + kept


def chat_completion(v1_base, model, messages, sampling, chat_template,
                    stream, caps):
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    int_keys = {"top_k", "repeat_last_n", "max_tokens", "seed"}
    for k, v in sampling.items():
        if v is None:
            continue
        if k == "seed" and v == -1:
            continue
        payload[k] = int(v) if k in int_keys and v is not None else v
    if chat_template:
        payload["chat_template"] = chat_template

    # Honor capability flags: some servers reject a system role.
    if caps and not caps.get("supports_system_role", True):
        payload["messages"] = fold_system(messages)

    try:
        r = requests.post(f"{v1_base}/chat/completions", json=payload,
                          stream=stream, timeout=600)
    except requests.RequestException as e:
        die(f"request failed: {e}")
    if r.status_code != 200:
        die(f"server returned {r.status_code}: {r.text[:500]}")

    if stream:
        full = []
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = obj.get("choices", [{}])[0].get("delta", {})
            piece = delta.get("content")
            if piece:
                print(piece, end="", flush=True)
                full.append(piece)
        print()
        return "".join(full)
    else:
        obj = r.json()
        return obj.get("choices", [{}])[0].get("message", {}).get("content", "")


def fold_system(messages):
    out = []
    sys_text = []
    for m in messages:
        if m["role"] == "system":
            sys_text.append(m.get("content", ""))
        else:
            out.append(m)
    if sys_text and out:
        out[0] = {"role": "user",
                  "content": "\n\n".join(sys_text) + "\n\n" + out[0].get("content", "")}
    return out


def banner(cfg, model, server_ctx, endpoint):
    print("=" * 60)
    print(f"  endpoint : {endpoint}")
    print(f"  model    : {model}")
    print(f"  server   : ctx={server_ctx}")
    print(f"  client   : ctx={cfg['context_length']}  stream={cfg['stream']}")
    s = cfg["sampling"]
    print("  sampling : " + " ".join(f"{k}={s[k]}" for k in SAMPLING_KEYS))
    print("=" * 60)
    print("Type /help for commands. Ctrl-C or /exit to quit.")


def show_config(cfg, model):
    print(json.dumps({"model": model, **cfg}, indent=2))


def main():
    base_cfg = load_config(CONFIG_PATH)
    args, cfg = parse_args(base_cfg)
    endpoint = cfg["endpoint"].rstrip("/")
    v1_base = endpoint if endpoint.endswith("/v1") else f"{endpoint}/v1"
    root_base = v1_base[:-3] if v1_base.endswith("/v1") else v1_base

    model = pick_model(v1_base, cfg.get("model"))
    props = fetch_props(root_base)

    server_ctx = props.get("default_generation_settings", {}).get("n_ctx") \
        or props.get("n_ctx") or 4096
    chat_template = props.get("chat_template")
    caps = props.get("chat_template_caps", {})

    # Baseline sampling = server defaults, then config, then CLI (already merged).
    baseline = normalize_props_sampling(props)
    sampling = deep_merge(baseline, cfg.get("sampling", {}))

    # Validate the pulled template compiles.
    if chat_template:
        try:
            build_jinja_env().from_string(chat_template)
        except Exception as e:
            print(f"warning: pulled chat_template failed to compile: {e}",
                  file=sys.stderr)

    # Context length clamps to the server's context window.
    ctx = cfg.get("context_length", 4096)
    if ctx and ctx > server_ctx:
        print(f"warning: context_length {ctx} > server n_ctx {server_ctx}; clamping.",
              file=sys.stderr)
        ctx = server_ctx
    cfg["context_length"] = ctx

    if args.render:
        msgs = []
        if cfg.get("system_prompt"):
            msgs.append({"role": "system", "content": cfg["system_prompt"]})
        if not any(m["role"] in ("user", "assistant") for m in msgs):
            msgs.append({"role": "user", "content": "(preview)"})
        print(render_preview(chat_template, msgs))
        return

    count = make_token_counter(root_base)
    messages = []
    if cfg.get("system_prompt"):
        messages.append({"role": "system", "content": cfg["system_prompt"]})

    banner(cfg, model, server_ctx, endpoint)

    while True:
        try:
            user_in = input("\nuser> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_in:
            continue
        if user_in.startswith("/"):
            parts = user_in.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd in ("/exit", "/quit"):
                print("bye.")
                break
            elif cmd == "/help":
                print("/help            show this help")
                print("/reset           clear conversation (keep system prompt)")
                print("/system <text>   set/show system prompt")
                print("/config          show current config + model")
                print("/set <k> <v>     live-override a sampling key")
                print("/render          preview the templated prompt")
                print("/exit /quit      quit")
            elif cmd == "/reset":
                messages = [{"role": "system", "content": cfg["system_prompt"]}] \
                    if cfg.get("system_prompt") else []
                print("conversation cleared.")
            elif cmd == "/system":
                if arg:
                    cfg["system_prompt"] = arg
                    if messages and messages[0]["role"] == "system":
                        messages[0]["content"] = arg
                    else:
                        messages.insert(0, {"role": "system", "content": arg})
                    print("system prompt updated.")
                else:
                    print(f"system: {cfg.get('system_prompt')!r}")
            elif cmd == "/config":
                show_config(cfg, model)
            elif cmd == "/set":
                sp = arg.split(None, 1)
                if len(sp) != 2 or sp[0] not in SAMPLING_KEYS:
                    print(f"usage: /set <{'|'.join(SAMPLING_KEYS)}> <value>")
                    continue
                key, val = sp
                try:
                    sampling[key] = int(val) if key in ("top_k", "repeat_last_n", "max_tokens", "seed") else float(val)
                    cfg.setdefault("sampling", {})[key] = sampling[key]
                    print(f"{key} = {sampling[key]}")
                except ValueError:
                    print("invalid numeric value")
            elif cmd == "/render":
                print(render_preview(chat_template, messages))
            else:
                print(f"unknown command: {cmd} (try /help)")
            continue

        messages.append({"role": "user", "content": user_in})

        budget = max(256, ctx - int(sampling.get("max_tokens", 2048) or 0))
        messages = trim_messages(messages, budget, count)

        print("assistant> ", end="", flush=True)
        reply = chat_completion(v1_base, model, messages, sampling,
                                chat_template, cfg["stream"], caps)
        messages.append({"role": "assistant", "content": reply or ""})


if __name__ == "__main__":
    main()
