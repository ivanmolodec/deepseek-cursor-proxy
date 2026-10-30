#!/usr/bin/env python3
"""Compare DeepSeek latency and throughput through the proxy vs the API directly.

The script sends the same kind of chat completion to both endpoints in
alternating order and reports medians, so network drift does not favour one
side. Every request carries a unique nonce, which keeps DeepSeek's context
cache out of the picture: both paths always pay for a cold prompt.

Standard library only, so it runs from the repository venv as-is:

    uv run python scripts/bench.py --stream --rounds 4

The proxy URL comes from --proxy-url, then $DCP_PROXY_URL, then the local
default. The API key comes from --api-key, then $DEEPSEEK_API_KEY, and on
Windows also from the user environment if the shell inherited an older copy.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

PROMPT = (
    "Объясни в пяти предложениях, почему дневное небо синее. "
    "Отвечай по-русски, без списков и заголовков. Метка запроса: "
)
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


def user_env(name: str) -> str:
    """Persisted Windows user variable, for shells started before it existed."""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
            return str(winreg.QueryValueEx(env, name)[0])
    except OSError:
        return ""


DEFAULT_PROXY_URL = (
    os.environ.get("DCP_PROXY_URL")
    or user_env("DCP_PROXY_URL")
    or "http://127.0.0.1:9000/v1"
)


def default_api_key() -> str:
    """Read the key from the environment, falling back to the user profile."""
    return os.environ.get("DEEPSEEK_API_KEY") or user_env("DEEPSEEK_API_KEY")


@dataclass
class Target:
    """One endpoint: a name for reports plus a parsed base URL."""

    name: str
    base_url: str
    scheme: str = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    path: str = field(init=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url.rstrip("/"))
        if parts.scheme not in ("http", "https"):
            raise SystemExit(f"{self.name}: unsupported scheme in {self.base_url!r}")
        if not parts.hostname:
            raise SystemExit(f"{self.name}: missing host in {self.base_url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        # The proxy is configured with the /v1 suffix, the bare DeepSeek host
        # serves /chat/completions directly. Accept either shape.
        self.path = parts.path.rstrip("/") + "/chat/completions"


@dataclass
class Sample:
    """Timings (seconds) and token counts of a single request."""

    connect: float
    headers: float
    total: float
    ttfb: float | None = None
    ttfb_content: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_chars: int = 0
    content_chars: int = 0
    tps: float | None = None


@dataclass
class Report:
    """Median summary over all samples of one target."""

    name: str
    samples: list[Sample] = field(default_factory=list)

    def median(self, attr: str) -> float | None:
        values = [
            getattr(s, attr) for s in self.samples if getattr(s, attr) is not None
        ]
        return statistics.median(values) if values else None

    def summary(self) -> dict[str, Any]:
        return {
            "target": self.name,
            "runs": len(self.samples),
            "connect_s": self.median("connect"),
            "headers_s": self.median("headers"),
            "ttfb_s": self.median("ttfb"),
            "ttfb_content_s": self.median("ttfb_content"),
            "total_s": self.median("total"),
            "tokens_per_s": self.median("tps"),
            "completion_tokens": self.median("completion_tokens"),
            "prompt_tokens": self.median("prompt_tokens"),
            "cached_tokens": self.median("cached_tokens"),
        }


def open_connection(target: Target) -> http.client.HTTPConnection:
    """Connect over TLS when needed and time it from the caller's start mark."""
    if target.scheme == "https":
        return http.client.HTTPSConnection(
            target.host,
            target.port,
            timeout=300,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(target.host, target.port, timeout=300)


def parse_sse(line: bytes) -> dict[str, Any] | None:
    """Return the JSON object of one SSE ``data:`` line, or None to stop."""
    if not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if payload == b"[DONE]":
        raise EOFError
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def read_stream(resp: http.client.HTTPResponse, start: float) -> dict[str, Any]:
    """Consume an SSE body, timing the first chunk and first visible token."""
    ttfb: float | None = None
    ttfb_content: float | None = None
    usage: dict[str, Any] = {}
    content: list[str] = []
    reasoning: list[str] = []

    for line in resp:
        now = time.perf_counter() - start
        try:
            data = parse_sse(line)
        except EOFError:
            break
        if data is None:
            continue
        if ttfb is None:
            ttfb = now
        if data.get("usage"):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            think = delta.get("reasoning_content") or ""
            if text:
                if ttfb_content is None:
                    ttfb_content = now
                content.append(text)
            if think:
                reasoning.append(think)

    return {
        "ttfb": ttfb,
        "ttfb_content": ttfb_content,
        "usage": usage,
        "content_chars": len("".join(content)),
        "reasoning_chars": len("".join(reasoning)),
    }


def ask(
    target: Target, api_key: str, model: str, max_tokens: int, stream: bool
) -> Sample:
    """Issue one chat completion and time the interesting milestones."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT + uuid.uuid4().hex}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    body = json.dumps(payload).encode()
    headers = dict(HEADERS, Authorization=f"Bearer {api_key}")

    start = time.perf_counter()
    conn = open_connection(target)
    conn.connect()
    connect = time.perf_counter() - start
    try:
        conn.request("POST", target.path, body=body, headers=headers)
        resp = conn.getresponse()
        headers_at = time.perf_counter() - start
        if resp.status != 200:
            detail = resp.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"{target.name}: HTTP {resp.status}: {detail}")

        if stream:
            data = read_stream(resp, start)
            sample = Sample(
                connect=connect,
                headers=headers_at,
                total=time.perf_counter() - start,
                ttfb=data["ttfb"],
                ttfb_content=data["ttfb_content"],
                reasoning_chars=data["reasoning_chars"],
                content_chars=data["content_chars"],
                **usage_fields(data["usage"]),
            )
        else:
            raw = resp.read()
            data = json.loads(raw)
            message = data["choices"][0]["message"]
            sample = Sample(
                connect=connect,
                headers=headers_at,
                total=time.perf_counter() - start,
                content_chars=len(message.get("content") or ""),
                reasoning_chars=len(message.get("reasoning_content") or ""),
                **usage_fields(data.get("usage") or {}),
            )
    finally:
        conn.close()

    sample.tps = tokens_per_second(sample, stream)
    return sample


def usage_fields(usage: dict[str, Any]) -> dict[str, int]:
    """Map DeepSeek/OpenAI usage keys onto the Sample fields."""
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "cached_tokens": usage.get("prompt_cache_hit_tokens")
        or usage.get("cached_tokens")
        or 0,
    }


def tokens_per_second(sample: Sample, stream: bool) -> float | None:
    """Output tokens per second, excluding the wait for the first byte.

    Streaming gives an honest window (first chunk -> last chunk). For
    non-streaming responses the proxy only releases headers once the body is
    complete, so headers == total there; fall back to the whole request and
    leave third-party comparisons to the total time.
    """
    if not sample.completion_tokens:
        return None
    began = sample.ttfb if stream else sample.headers
    if began is None:
        return None
    generating = sample.total - began
    if generating <= 0.05:
        return None
    return sample.completion_tokens / generating


def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    """Format an optional float for the console table."""
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def print_sample(name: str, round_no: int, sample: Sample, stream: bool) -> None:
    """Print one request so a slow outlier is visible, not hidden by medians."""
    first = sample.ttfb if stream else None
    first_content = sample.ttfb_content if stream else None
    print(
        f"  {name} #{round_no}: total={fmt(sample.total, 3, 's')} "
        f"connect={fmt(sample.connect, 3, 's')} "
        f"headers={fmt(sample.headers, 3, 's')} "
        f"ttfb={fmt(first, 3, 's')} "
        f"first_content={fmt(first_content, 3, 's')} "
        f"in={sample.prompt_tokens} out={sample.completion_tokens} "
        f"cached={sample.cached_tokens} tps={fmt(sample.tps, 1)}",
        flush=True,
    )


def warmup(target: Target, api_key: str, model: str) -> None:
    """Pay TLS and upstream cold-start costs before the measured rounds."""
    print(f"warmup {target.name} ...", flush=True)
    try:
        ask(target, api_key, model, max_tokens=8, stream=False)
    except Exception as exc:  # noqa: BLE001 - a failed warmup should not abort the run
        print(f"  warmup failed: {exc}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark DeepSeek via the proxy against the direct API.",
    )
    parser.add_argument(
        "--proxy-url",
        default=DEFAULT_PROXY_URL,
        help="proxy base URL (default: $DCP_PROXY_URL or %(default)s)",
    )
    parser.add_argument(
        "--direct-url",
        default="https://api.deepseek.com/v1",
        help="direct API base URL (default: %(default)s)",
    )
    parser.add_argument("--model", default="deepseek-v4-pro", help="model name")
    parser.add_argument(
        "--rounds", type=int, default=3, help="measured requests per path"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=400,
        help="completion cap; reasoning tokens count towards it",
    )
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use server-sent events (default: on)",
    )
    parser.add_argument(
        "--api-key",
        default=default_api_key(),
        help="API key (default: $DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--only",
        choices=("proxy", "direct"),
        help="benchmark just one side, e.g. to recheck a suspect path",
    )
    parser.add_argument(
        "--json", action="store_true", help="print only the JSON summary"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.api_key:
        print("error: set DEEPSEEK_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    if args.rounds < 1:
        print("error: --rounds must be at least 1", file=sys.stderr)
        return 2

    targets = [
        Target("proxy", args.proxy_url),
        Target("direct", args.direct_url),
    ]
    if args.only:
        targets = [t for t in targets if t.name == args.only]

    mode = "stream" if args.stream else "non-stream"
    if not args.json:
        print(
            f"model={args.model} mode={mode} rounds={args.rounds} "
            f"max_tokens={args.max_tokens}"
        )
        for target in targets:
            print(f"  {target.name}: {target.base_url}")

    for target in targets:
        warmup(target, args.api_key, args.model)

    reports = {t.name: Report(t.name) for t in targets}
    for round_no in range(1, args.rounds + 1):
        # Alternate the order so a transient slowdown does not always hit the
        # same side; both endpoints see the same number of "first" requests.
        order = targets if round_no % 2 else list(reversed(targets))
        for target in order:
            try:
                sample = ask(
                    target,
                    args.api_key,
                    args.model,
                    args.max_tokens,
                    args.stream,
                )
            except Exception as exc:  # noqa: BLE001 - report and keep the run alive
                print(f"  {target.name} #{round_no} failed: {exc}", file=sys.stderr)
                continue
            reports[target.name].samples.append(sample)
            if not args.json:
                print_sample(target.name, round_no, sample, args.stream)

    summaries = {name: rep.summary() for name, rep in reports.items()}
    if args.json:
        print(json.dumps(summaries, indent=2, ensure_ascii=False))
    elif all(rep.samples for rep in reports.values()):
        print("\n=== median ===")
        for summary in summaries.values():
            print(
                f"{summary['target']:6} total={fmt(summary['total_s'], 3, 's')} "
                f"headers={fmt(summary['headers_s'], 3, 's')} "
                f"ttfb={fmt(summary['ttfb_s'], 3, 's')} "
                f"first_content={fmt(summary['ttfb_content_s'], 3, 's')} "
                f"tokens_per_s={fmt(summary['tokens_per_s'], 1)}"
            )
        if len(summaries) == 2:
            proxy, direct = summaries["proxy"], summaries["direct"]
            for key, label in (
                ("total_s", "total"),
                ("ttfb_s", "first byte"),
                ("tokens_per_s", "tokens/s"),
            ):
                a, b = proxy[key], direct[key]
                if a and b:
                    print(f"  proxy vs direct {label}: " f"{(a / b - 1) * 100:+.1f}%")

    return 0 if all(rep.samples for rep in reports.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
