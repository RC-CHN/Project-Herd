"""HTTP transport for OpenAI-compatible inference and tokenization."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request as HttpRequest, build_opener

from ...model import encode
from ..prefix import message_key

TRANSIENT_HTTP = frozenset((429, 500, 502, 503, 504))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ChatTransport:
    """Same-host transport and prompt key generation for Chat Completions servers."""

    def __init__(self, base_url, model, tokenizer_path, secret):
        parts = urlsplit(base_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.port == 0
            or any(ch.isspace() for ch in base_url)
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError("base URL must be an HTTP(S) API root such as http://host:port/v1")
        if tokenizer_path and (not tokenizer_path.startswith("/") or tokenizer_path.startswith("//")):
            raise ValueError("tokenizer path must start with a single / on the same server")
        self.completions = base_url.rstrip("/") + "/chat/completions"
        self.tokenizer = f"{parts.scheme}://{parts.netloc}{tokenizer_path}" if tokenizer_path else None
        self.unit = "tokens" if self.tokenizer else "bytes"
        self.model = model
        self.secret = secret

    def post(self, url, body):
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        payload = encode(body).encode()
        max_attempts = 6
        for attempt in range(max_attempts):
            request = HttpRequest(url, data=payload, headers=headers)
            try:
                with build_opener(NoRedirect).open(request, timeout=240) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code in TRANSIENT_HTTP and attempt < max_attempts - 1:
                    delay = min(60.0, float(2 * (2**attempt)))
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after and retry_after.strip().isdigit():
                        delay = min(60.0, max(delay, float(retry_after.strip())))
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{urlsplit(url).path} returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 3:
                    time.sleep(2 * (2**attempt))
                    continue
                reason = exc.reason if isinstance(exc, URLError) else exc
                raise RuntimeError(f"could not reach {urlsplit(url).netloc}: {reason}") from exc

    def prompt_key(self, request, tools=None):
        if self.tokenizer is None:
            return message_key(request.messages)
        body = {"model": self.model, "messages": request.messages, "add_generation_prompt": True}
        if tools is not None:
            body["tools"] = tools
        result = self.post(self.tokenizer, body)
        ids = result.get("tokens") if isinstance(result, dict) else None
        if not ids or not isinstance(ids, list) or any(type(t) is not int or t < 0 for t in ids):
            raise ValueError("tokenizer did not return an array of token IDs")
        return tuple(ids)
