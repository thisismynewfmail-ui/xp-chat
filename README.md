# TACHI-COM v2.1 — Section 9 Intranet Terminal

An XP-era, Ghost in the Shell: SAC-flavored web chat console for any
**OpenAI-compatible endpoint** (llama.cpp server, LM Studio, vLLM, ollama, …).
Python standard library only — no pip installs. Runs on Windows and Linux.

```
python python.py            # starts the server and opens your browser
python python.py --port 9000 --no-browser
```

The entry point is `python.py`. The UI spawns at `http://127.0.0.1:8484/`.

## What it does

- **Full XP desktop UI** — draggable windows, taskbar + Start menu, boot
  splash, desktop icons, CRT scanlines, "the net" glyph-rain backdrop, a
  live spectrum analyzer wired to the token stream, and a Tachikoma mascot
  whose eye lights up while the model generates.
- **Streaming chat** with a blinking cursor, markdown rendering, per-message
  copy, regenerate, and correct quote/HTML escaping everywhere.
- **Sampling settings** passed to the endpoint verbatim with exactly these
  keys: `temperature`, `top_p`, `top_k`, `min_p`, `typical_p`,
  `repeat_penalty`, `repeat_last_n`, `presence_penalty`, `frequency_penalty`,
  `max_tokens`, `seed`. Server-side defaults are **pulled from the endpoint**
  (`/props` on llama.cpp) and can be applied with one click.
- **System message** set + saved in the settings menu; sent as the `system`
  role so the chat template renders it; survives every context trim.
- **Thinking support** — `enable_thinking` toggle passed via
  `chat_template_kwargs`; handles both `reasoning_content` deltas and inline
  `<think>…</think>` tags (even split across stream chunks). Thinking is
  compacted by default, expandable per message, or hideable entirely.
- **Prompt / instruct templates** — the model's own Jinja template is pulled
  from the endpoint and shown in Settings ▸ Template; you can override it
  with built-in presets (ChatML/Qwen, Qwen3 thinking, Llama 2/3, Mistral,
  Vicuna, Alpaca, Gemma, Phi, DeepSeek-R1, Zephyr) or a fully custom Jinja
  template, sent upstream as `chat_template`.
- **"User" is the username** — displayed in chat and passed on user messages
  as the OpenAI `name` field (configurable, defaults to `User`). The bot's
  display name is configurable too.
- **Endpoint testing** — Test-connection button plus a live green/red link
  LED (status bar and system tray) with latency readout.
- **Context limit handling** — context length setting (default **8196**).
  When the prompt exceeds the trim threshold (default **75%** full), whole
  messages fall cleanly out of the middle of the conversation (right after
  the system message, oldest first); the system message and the most recent
  messages are always kept, and trimmed history always restarts on a user
  turn. Exact token counts come from `/tokenize` when the endpoint offers
  it. A gold "DEEP ARCHIVE" divider marks what fell out of context.
- **Sessions** — save, copy (duplicate), rename, delete, clear, and export
  (.json / .txt) chat sessions.
- **Everything is persistent and synced** — chats and settings live
  server-side in `./data/`; every open view syncs live over SSE, including
  in-flight token streams.

## Defaults

Seed defaults are gathered from the bundled config file — `.config` if
present, else `config.json` — then overlaid with anything saved from the
settings menu (stored in `data/settings.json`).

## Layout

```
python.py            entry point (server + browser launch)
xpchat/config.py     settings load/merge/persist
xpchat/llm.py        upstream client, sampling payload, think-tag splitter
xpchat/contextman.py context-window trimming
xpchat/store.py      chat persistence + SSE event bus
xpchat/server.py     HTTP/SSE server + generation manager
xpchat/templates.py  built-in Jinja instruct templates
static/              the XP desktop UI (index.html, xp.css, app.js)
```
