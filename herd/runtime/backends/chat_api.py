"""API backend for bounded tool conversations and flat JSON decisions."""

import hashlib
import json
import math
import os
from urllib.error import HTTPError

from ...model import ACTIONS, FIELD_LIMITS, QTY_PATTERN, encode
from .base import Backend, _text, _positive_int
from .http import ChatTransport


class ChatAPI(ChatTransport, Backend):
    def __init__(
        self,
        base_url,
        model,
        max_tokens=1536,
        tokenizer_path=None,
        cache_key=None,
        *,
        api_key_env="HERD_API_KEY",
        api_key=None,
        workers=None,
        context_bytes=None,
        name="api",
        mode="agent_loop",
        max_steps=6,
        temperature=0.3,
        frequency_penalty=None,
        preserve_reasoning=False,
    ):
        for field, value in (
            ("base_url", base_url),
            ("model", model),
            ("api_key_env", api_key_env),
            ("profile name", name),
        ):
            _text(value, field)
        for field, value in (
            ("tokenizer_path", tokenizer_path),
            ("cache_key", cache_key),
            ("api_key", api_key),
        ):
            if value is not None:
                _text(value, field)
        _positive_int(max_tokens, "max_tokens")
        if workers is not None:
            _positive_int(workers, "workers")
        if context_bytes is not None:
            _positive_int(context_bytes, "context_bytes")
        self.name, self.workers, self.context_bytes = name, workers, context_bytes
        if mode not in ("agent_loop", "json"):
            raise ValueError("API mode must be agent_loop or json")
        _positive_int(max_steps, "max_steps")
        self.mode, self.max_steps = mode, max_steps
        if temperature is not None:
            if (
                isinstance(temperature, bool)
                or type(temperature) not in (int, float)
                or not math.isfinite(temperature)
                or not 0.0 <= temperature <= 2.0
            ):
                raise ValueError("temperature must be null or a number in [0, 2]")
            temperature = float(temperature)
        self.temperature = temperature
        if frequency_penalty is not None:
            if (
                isinstance(frequency_penalty, bool)
                or type(frequency_penalty) not in (int, float)
                or not math.isfinite(frequency_penalty)
                or not -2.0 <= frequency_penalty <= 2.0
            ):
                raise ValueError("frequency_penalty must be null or a number in [-2, 2]")
            frequency_penalty = float(frequency_penalty)
        self.frequency_penalty = frequency_penalty
        if not isinstance(preserve_reasoning, bool):
            raise ValueError("preserve_reasoning must be a boolean")
        self.preserve_reasoning = preserve_reasoning
        super().__init__(base_url, model, tokenizer_path, api_key or os.environ.get(api_key_env))
        self.max_tokens = max_tokens
        self.cache_key = cache_key
        namespace = [self.completions, model, self.tokenizer, cache_key, "message-framing-v1"]
        if name != "api" or api_key_env != "HERD_API_KEY":
            namespace.extend([name, api_key_env])
        if mode == "agent_loop":
            namespace.append(mode)
        if self.temperature != 0.3:
            namespace.extend(["temperature", self.temperature])
        if self.frequency_penalty is not None:
            namespace.extend(["frequency_penalty", self.frequency_penalty])
        self.scope = hashlib.sha256(encode(namespace).encode()).hexdigest()

    def key(self, request):
        if self.tokenizer and self.mode == "agent_loop":
            from ..agent.schemas import TOOL_SCHEMAS

            return self.prompt_key(request, tools=TOOL_SCHEMAS)
        return self.prompt_key(request)

    def decide(self, request):
        return self._complete(request, {"type": "json_object"})

    def retry_format(self, request, error=None, previous_reply=None, attempt=1):
        fields = {k: {"type": "string", "maxLength": limit} for k, limit in FIELD_LIMITS.items()}
        fields["action"] = {"type": "string", "enum": ACTIONS}
        fields["id"] = {"type": "string", "const": request.actor}
        fields["qty"]["pattern"] = QTY_PATTERN
        schema = {
            "type": "object",
            "properties": fields,
            "required": ["action"],
            "additionalProperties": False,
        }
        extra = None
        if error:
            extra = []
            if previous_reply:
                extra.append({"role": "assistant", "content": previous_reply})
            extra.append(
                {
                    "role": "user",
                    "content": (
                        f"上次输出的 JSON 校验未通过：{error}。"
                        '所有字段值必须为字符串（例如 qty 写成 "1"）；不用的可选字段请直接省略，'
                        "不要只提供 whisper 或 whisper_to 之一，也不要提供 note 而不提供 note_key。"
                        "请重新输出一个合法的扁平 JSON 对象。"
                    ),
                }
            )
        if attempt >= 2:
            return self._complete(request, {"type": "json_object"}, extra)
        try:
            return self._complete(
                request,
                {"type": "json_schema", "json_schema": {"name": "intent", "schema": schema}},
                extra,
            )
        except RuntimeError as exc:
            if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code in (400, 422):
                return self._complete(request, {"type": "json_object"}, extra)
            raise

    def chat(self, messages, tools=None, response_format=None):
        """Return a native assistant message and usage, including content-free tool calls."""
        actor = None
        if self.cache_key and "{actor}" in self.cache_key:
            first_user = next((m.get("content") for m in messages if m.get("role") == "user"), None)
            # ResidentAgent puts the world's resident ID on the first line of
            # this message. The same immutable prefix is present at every loop step.
            if isinstance(first_user, str):
                try:
                    identity = json.loads(first_user.partition("\n")[0])
                except ValueError:
                    identity = None
                if isinstance(identity, dict):
                    actor = identity.get("id")
            if not isinstance(actor, str) or not actor:
                raise ValueError("per-resident cache_key requires a resident prompt")
        choice, metrics = self._request(messages, actor, tools, response_format)
        return choice["message"], metrics

    def _complete(self, request, response_format, extra_messages=None):
        messages = request.messages + extra_messages if extra_messages else request.messages
        choice, metrics = self._request(messages, request.actor, None, response_format)
        reply = choice["message"].get("content")
        if choice.get("finish_reason") == "length" or not isinstance(reply, str) or not reply.strip():
            raise ValueError("incomplete model reply; retry or increase --max-tokens")
        return reply, metrics

    def _request(self, messages, actor, tools, response_format):
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.frequency_penalty is not None:
            body["frequency_penalty"] = self.frequency_penalty
        if response_format is not None:
            body["response_format"] = response_format
        if tools is not None:
            body["tools"] = tools
        if self.cache_key:
            body["prompt_cache_key"] = self.cache_key.replace("{actor}", actor or "")
        try:
            result = self.post(self.completions, body)
        except RuntimeError as exc:
            if (
                tools is not None
                and isinstance(exc.__cause__, HTTPError)
                and exc.__cause__.code in (400, 422)
            ):
                raise RuntimeError(
                    f"{self.name}: server rejected native tools (HTTP {exc.__cause__.code})"
                ) from exc
            raise
        choices = result.get("choices") if isinstance(result, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("inference server returned no assistant message")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            raise ValueError("expected an assistant message")
        message.setdefault("role", "assistant")
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("expected a token usage object")
        prompt_details = usage.get("prompt_tokens_details") or {}
        if not isinstance(prompt_details, dict):
            raise ValueError("expected a prompt token usage object")
        metrics = {
            "profile": self.name,
            "model": self.model,
            "mode": self.mode,
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": prompt_details.get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        return choice, metrics
