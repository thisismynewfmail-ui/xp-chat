"""Context-window management.

Keeps the system message plus the most recent conversation inside the
configured context window. When the prompt would exceed the trim threshold
(default 75% of the context length), whole messages are dropped from the
middle of the conversation — right after the system message — oldest first,
so the cut is always clean (never mid-message) and the trimmed history still
starts on a user turn.
"""

MSG_OVERHEAD = 6      # rough per-message template overhead in tokens
GEN_MARGIN = 64       # safety margin reserved on top of max_tokens


def budget_for(settings):
    ctx = int(settings.get("context_length") or 8196)
    fill = float(settings.get("context_fill_ratio") or 0.75)
    max_tokens = int((settings.get("sampling") or {}).get("max_tokens") or 1024)
    budget = int(ctx * fill)
    # the prompt must also physically fit alongside the reply
    budget = min(budget, ctx - max_tokens - GEN_MARGIN)
    return max(256, budget), ctx


def trim_messages(messages, counter, settings):
    """messages: [{role, content, ...}] including the system message.

    Returns (kept_messages, dropped_count, used_tokens, budget).
    counter(text) -> token count.
    """
    budget, _ctx = budget_for(settings)
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") != "system"]

    def cost(m):
        return counter(m.get("content") or "") + MSG_OVERHEAD

    used = sum(cost(m) for m in sys_msgs)
    kept = []
    for m in reversed(convo):
        c = cost(m)
        if kept and used + c > budget:
            break
        kept.append(m)
        used += c
    if not kept and convo:          # always keep at least the latest message
        kept = [convo[-1]]
        used += cost(convo[-1])
    kept.reverse()

    # clean cut: history must open on a user turn, not an orphaned reply
    while kept and kept[0].get("role") == "assistant":
        used -= cost(kept[0])
        kept.pop(0)

    dropped = len(convo) - len(kept)
    return sys_msgs + kept, dropped, used, budget
