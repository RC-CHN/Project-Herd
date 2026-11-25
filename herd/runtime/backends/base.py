"""Inference interfaces shared by routed and single-profile simulations."""

from abc import ABC, abstractmethod


class Backend(ABC):
    """The prompt key and the completion must address the same inference cache."""

    name: str
    model: str
    unit: str
    scope: str
    workers: int | None = None
    mode: str = "json"
    max_steps: int = 6

    @abstractmethod
    def key(self, request):
        pass

    def chat(self, messages, tools=None, response_format=None):
        raise NotImplementedError("backend does not implement native chat")

    def decide(self, request):
        raise NotImplementedError("backend does not implement single-step decisions")

    def retry_format(self, request, error=None, previous_reply=None, attempt=1):
        return self.decide(request)


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def _positive_int(value, field):
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} must be a positive integer")
