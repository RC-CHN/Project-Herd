"""Dispatch neighbours in prompt-prefix order. The inference server owns the KV tensors."""

from bisect import bisect_left

from ..model import encode


def message_key(messages):
    # Framed messages are unambiguous and remain a prefix when a new turn is appended.
    parts = []
    for m in messages:
        item = [m["role"], m["content"]]
        if "tool_calls" in m:
            item.append(m["tool_calls"])
        if "tool_call_id" in m:
            item.append(m["tool_call_id"])
        parts.append(encode(item).encode() + b"\n")
    return b"".join(parts)


def common_prefix(left, right):
    lo, hi = 0, min(len(left), len(right))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if left[:mid] == right[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


class PrefixQueue:
    def __init__(self, keyed_requests, anchors):
        self.requests = {req.actor: req for _, req in keyed_requests}
        self.keys = sorted((key, req.actor) for key, req in keyed_requests)
        self.anchors = anchors

    def pop(self):
        # A longest prefix in a sorted set must occur at a predecessor or successor.
        best_index, best_len = 0, 0
        for anchor in reversed(self.anchors):
            pos = bisect_left(self.keys, (anchor, ""))
            for index in (pos, pos - 1):
                if 0 <= index < len(self.keys):
                    length = common_prefix(anchor, self.keys[index][0])
                    if length > best_len:
                        best_index, best_len = index, length
        key, actor = self.keys.pop(best_index)
        return self.requests.pop(actor), key, best_len
