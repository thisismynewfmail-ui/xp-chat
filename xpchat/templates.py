"""Built-in chat/instruct template presets (Jinja).

These are passed verbatim to the endpoint as the `chat_template` field of the
/v1/chat/completions payload (llama.cpp applies them server-side). The "auto"
template mode skips this field entirely so the template pulled from the model
is used.
"""

CHATML = (
    "{%- for message in messages -%}"
    "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<|im_start|>assistant\n{%- endif -%}"
)

QWEN3 = (
    "{%- for message in messages -%}"
    "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}"
    "<|im_start|>assistant\n"
    "{%- if enable_thinking is defined and enable_thinking is false -%}"
    "<think>\n\n</think>\n\n"
    "{%- endif -%}"
    "{%- endif -%}"
)

LLAMA3 = (
    "{%- for message in messages -%}"
    "<|start_header_id|>{{ message.role }}<|end_header_id|>\n\n"
    "{{ message.content }}<|eot_id|>"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}"
    "<|start_header_id|>assistant<|end_header_id|>\n\n{%- endif -%}"
)

LLAMA2 = (
    "{%- set ns = namespace(sys='') -%}"
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{%- set ns.sys = message.content -%}"
    "{%- elif message.role == 'user' -%}"
    "{%- if ns.sys -%}[INST] <<SYS>>\n{{ ns.sys }}\n<</SYS>>\n\n{{ message.content }} [/INST]"
    "{%- set ns.sys = '' -%}"
    "{%- else -%}[INST] {{ message.content }} [/INST]{%- endif -%}"
    "{%- elif message.role == 'assistant' -%} {{ message.content }} </s><s>"
    "{%- endif -%}"
    "{%- endfor -%}"
)

MISTRAL = (
    "{%- set ns = namespace(sys='') -%}"
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{%- set ns.sys = message.content -%}"
    "{%- elif message.role == 'user' -%}"
    "{%- if ns.sys -%}[INST] {{ ns.sys }}\n\n{{ message.content }} [/INST]"
    "{%- set ns.sys = '' -%}"
    "{%- else -%}[INST] {{ message.content }} [/INST]{%- endif -%}"
    "{%- elif message.role == 'assistant' -%}{{ message.content }}</s>"
    "{%- endif -%}"
    "{%- endfor -%}"
)

VICUNA = (
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{{ message.content }}\n\n"
    "{%- elif message.role == 'user' -%}USER: {{ message.content }}\n"
    "{%- elif message.role == 'assistant' -%}ASSISTANT: {{ message.content }}</s>\n"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}ASSISTANT:{%- endif -%}"
)

ALPACA = (
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{{ message.content }}\n\n"
    "{%- elif message.role == 'user' -%}### Instruction:\n{{ message.content }}\n\n"
    "{%- elif message.role == 'assistant' -%}### Response:\n{{ message.content }}\n\n"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}### Response:\n{%- endif -%}"
)

GEMMA = (
    "{%- set ns = namespace(sys='') -%}"
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{%- set ns.sys = message.content -%}"
    "{%- elif message.role == 'user' -%}"
    "<start_of_turn>user\n"
    "{%- if ns.sys -%}{{ ns.sys }}\n\n{%- set ns.sys = '' -%}{%- endif -%}"
    "{{ message.content }}<end_of_turn>\n"
    "{%- elif message.role == 'assistant' -%}"
    "<start_of_turn>model\n{{ message.content }}<end_of_turn>\n"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<start_of_turn>model\n{%- endif -%}"
)

PHI = (
    "{%- for message in messages -%}"
    "<|{{ message.role }}|>\n{{ message.content }}<|end|>\n"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<|assistant|>\n{%- endif -%}"
)

DEEPSEEK_R1 = (
    "{%- set ns = namespace(sys='') -%}"
    "{%- for message in messages -%}"
    "{%- if message.role == 'system' -%}{%- set ns.sys = ns.sys + message.content -%}"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{{ ns.sys }}"
    "{%- for message in messages -%}"
    "{%- if message.role == 'user' -%}<｜User｜>{{ message.content }}"
    "{%- elif message.role == 'assistant' -%}<｜Assistant｜>{{ message.content }}<｜end▁of▁sentence｜>"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<｜Assistant｜><think>\n{%- endif -%}"
)

ZEPHYR = (
    "{%- for message in messages -%}"
    "<|{{ message.role }}|>\n{{ message.content }}</s>\n"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<|assistant|>\n{%- endif -%}"
)

PRESETS = {
    "ChatML (Qwen)": CHATML,
    "Qwen3 (thinking)": QWEN3,
    "Llama 3.x Instruct": LLAMA3,
    "Llama 2 Chat": LLAMA2,
    "Mistral Instruct": MISTRAL,
    "Vicuna": VICUNA,
    "Alpaca": ALPACA,
    "Gemma": GEMMA,
    "Phi": PHI,
    "DeepSeek-R1": DEEPSEEK_R1,
    "Zephyr": ZEPHYR,
}


def resolve_template(settings, pulled_template):
    """Return the chat_template string to send upstream, or None for auto."""
    mode = settings.get("template_mode", "auto")
    if mode == "custom" and (settings.get("template_custom") or "").strip():
        return settings["template_custom"]
    if mode == "preset":
        return PRESETS.get(settings.get("template_preset") or "", None)
    return None  # auto: let the server use the template pulled from the model
