"""Resolve one resident's decision without advancing or modifying the world."""

from dataclasses import asdict
from inspect import signature

from ...model import Intent, encode
from ..storage.notes import note_ops_for
from .schemas import TOOL_SCHEMAS
from .toolbox import AgentToolbox
from .tools import ACTIONS


TOKEN_METRICS = ("prompt_tokens", "cached_tokens", "completion_tokens")


def canonical_reply(intent):
    return encode({key: value for key, value in asdict(intent).items() if value})


def add_usage(totals, usage):
    for key in TOKEN_METRICS:
        value = usage.get(key)
        if type(value) is int:
            totals[key] = (totals.get(key) or 0) + value
        elif key in usage:
            totals.setdefault(key, None)


def turn_metrics(backend):
    return {
        "profile": backend.name,
        "model": backend.model,
        "mode": backend.mode,
        "loop_steps": 0,
        "tool_calls": [],
    }


def tool_message(assistant, step, *, preserve_reasoning=False):
    """Ensure each response can refer to its assistant call and strip non-standard fields."""
    content = assistant.get("content")
    message = {"role": "assistant", "content": content if isinstance(content, str) else ""}
    # Opt-in for thinking models (e.g., Kimi/DeepSeek-R1) that require reasoning_content inside a turn's tool loop.
    if preserve_reasoning:
        reasoning = assistant.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            message["reasoning_content"] = reasoning
    raw_calls = assistant.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raw_calls = [raw_calls]
    calls = []
    ids = set()
    for index, raw in enumerate(raw_calls):
        call = dict(raw) if isinstance(raw, dict) else {}
        function = call.get("function")
        if call.get("type", "function") != "function" or not isinstance(function, dict):
            function = {}
        name = function.get("name")
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = encode(arguments)
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            call_id = f"call-{step}-{index}"
            while call_id in ids:
                call_id += "-"
        ids.add(call_id)
        calls.append(
            {
                "type": "function",
                "id": call_id,
                "function": {
                    "name": name if isinstance(name, str) and name else "invalid_tool_call",
                    "arguments": arguments,
                },
            }
        )
    if calls:
        message["tool_calls"] = calls
    return message, calls


FINAL_STEP_HINT = (
    "下一步是最后一步，请直接调用一个世界行动工具提交本轮决定"
    "（可在同一步并行调用 stage_speech、stage_whisper 或 write_note）。"
)


def with_budget(result, remaining):
    enriched = dict(result, remaining_steps=remaining)
    if remaining == 1:
        enriched["hint"] = FINAL_STEP_HINT
    return enriched


class AgentLoop:
    def __init__(self, backend, store):
        self.backend = backend
        self.store = store

    def run_turn(self, request):
        box = AgentToolbox(request, self.store)
        messages = list(request.messages)
        metrics = turn_metrics(self.backend)
        for step in range(1, self.backend.max_steps + 1):
            remaining = self.backend.max_steps - step
            assistant, usage = self.backend.chat(list(messages), tools=TOOL_SCHEMAS)
            add_usage(metrics, usage)
            metrics["loop_steps"] = step
            assistant, calls = tool_message(
                assistant, step, preserve_reasoning=getattr(self.backend, "preserve_reasoning", False)
            )
            messages.append(assistant)
            if not calls:
                prompt = f"请调用工具查询信息或提交本轮行动（剩余步数：{remaining}）。"
                if remaining == 1:
                    prompt += FINAL_STEP_HINT
                messages.append({"role": "user", "content": prompt})
                continue
            action_indices = [i for i, call in enumerate(calls) if call["function"]["name"] in ACTIONS]
            if len(action_indices) > 1:
                error = with_budget(
                    {"ok": False, "error": "choose at most one world action per turn"},
                    remaining,
                )
                for call in calls:
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": encode(error)})
                continue
            whisper_indices = [i for i, call in enumerate(calls) if call["function"]["name"] == "stage_whisper"]
            if len(whisper_indices) > 1 and remaining > 0:
                error = with_budget(
                    {
                        "ok": False,
                        "error": (
                            "at most one stage_whisper per turn (up to 4 targets in a single call); "
                            "merge recipients into one stage_whisper or save others for next turn"
                        ),
                    },
                    remaining,
                )
                for call in calls:
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": encode(error)})
                continue
            # Run internal tools (notes, speech, whisper, reads) before a parallel world action
            # so staged speech and notes always accompany the committed action regardless of order.
            order = [i for i in range(len(calls)) if i not in action_indices] + action_indices
            results = {}
            for index in order:
                function = calls[index]["function"]
                result = box.call(function["name"], function["arguments"])
                results[index] = result if box.intent is not None and index in action_indices else with_budget(result, remaining)
            for index, call in enumerate(calls):
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": encode(results[index])}
                )
            if box.intent is not None:
                break
        if box.intent is None:
            box.intent = box.parse_intent({"action": "wait"})
            metrics["fallback_wait"] = True
        metrics.update(note_ops=box.note_ops, tool_calls=box.tool_calls)
        return canonical_reply(box.intent), box.note_ops, metrics


class Agent:
    def __init__(self, backend, store):
        self.backend = backend
        self.loop = AgentLoop(backend, store) if backend.mode == "agent_loop" else None

    def run_turn(self, request):
        if self.loop:
            return self.loop.run_turn(request)
        metrics = turn_metrics(self.backend)
        previous_reply, error = None, None
        for attempt in range(3):
            reply = None
            try:
                if attempt:
                    retry = self.backend.retry_format
                    feedback = (request, str(error), previous_reply, attempt)
                    # Earlier single-step adapters accepted just the original request.
                    try:
                        signature(retry).bind(*feedback)
                    except TypeError:
                        reply, usage = retry(request)
                    else:
                        reply, usage = retry(*feedback)
                else:
                    reply, usage = self.backend.decide(request)
                add_usage(metrics, usage)
                if not isinstance(reply, str):
                    raise ValueError("expected a flat JSON intent as text")
                intent = Intent.parse(reply, request.actor, request.frame["view"])
            except ValueError as exc:
                previous_reply, error = reply, exc
                continue
            ops = note_ops_for(intent, {})
            # Adapters may attach accounting beyond the shared token counters.
            for key, value in usage.items():
                if key not in metrics and key not in TOKEN_METRICS:
                    metrics[key] = value
            metrics.update(attempts=attempt + 1, note_ops=ops)
            return canonical_reply(intent), ops, metrics
        raise ValueError(f"{request.actor}: invalid intent after three attempts: {error}") from error
