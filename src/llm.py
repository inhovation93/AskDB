"""Anthropic Claude 호출 래퍼.

관심사:
  · 모델/effort 선택과 프롬프트 캐싱 적용
  · 토큰 사용량·비용·지연시간 계측 (관측성이 없으면 운영에 못 올린다)
  · 오류를 사용자에게 보여줄 수 있는 한국어 메시지로 변환
  · <sql> 같은 태그 파싱
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import anthropic

# 요금(USD / 1M tokens) — 사용량 대시보드 표시용
MODELS: dict[str, dict] = {
    "claude-opus-5": {"label": "Claude Opus 5 (최고 정확도)", "in": 5.0, "out": 25.0},
    "claude-sonnet-5": {"label": "Claude Sonnet 5 (빠름·저비용)", "in": 2.0, "out": 10.0},
}
DEFAULT_MODEL = "claude-opus-5"
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


class LLMError(Exception):
    """사용자에게 그대로 보여줘도 되는 한국어 오류."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    calls: int = 0
    latency_sec: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.calls += other.calls
        self.latency_sec += other.latency_sec

    def cost_usd(self, model: str) -> float:
        p = MODELS.get(model, MODELS[DEFAULT_MODEL])
        return (
            self.input_tokens * p["in"]
            + self.cache_write * p["in"] * CACHE_WRITE_MULT
            + self.cache_read * p["in"] * CACHE_READ_MULT
            + self.output_tokens * p["out"]
        ) / 1_000_000


@dataclass
class LLMResponse:
    text: str
    usage: Usage
    stop_reason: str | None = None
    raw_blocks: list = field(default_factory=list)


def make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)


def _extract_usage(resp, latency: float) -> Usage:
    u = resp.usage
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        calls=1,
        latency_sec=latency,
    )


def complete(client: anthropic.Anthropic, *, model: str, system: list[dict] | str,
             messages: list[dict], max_tokens: int = 8000,
             effort: str = "medium", cache_system: bool = True) -> LLMResponse:
    # max_tokens 는 adaptive thinking 토큰까지 함께 소진하므로 넉넉히 잡는다.
    # 부족하면 stop_reason='max_tokens' 로 SQL 이 잘려 나와 자기수정 루프를 낭비한다.
    """단일 호출. system 의 마지막 블록에 캐시 브레이크포인트를 건다."""
    if cache_system and isinstance(system, list) and system:
        system = [dict(b) for b in system]
        system[-1]["cache_control"] = {"type": "ephemeral"}

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if effort:
        # effort 는 output_config 안에 들어간다 (top-level 아님).
        kwargs["output_config"] = {"effort": effort}

    started = time.time()
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise LLMError("API 키가 유효하지 않습니다. 사이드바에서 키를 다시 확인해 주세요.") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("요청이 몰려 잠시 제한되었습니다(429). 몇 초 후 다시 시도해 주세요.") from exc
    except anthropic.APITimeoutError as exc:
        raise LLMError("모델 응답이 지연되어 시간 초과되었습니다. 다시 시도해 주세요.") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("네트워크 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"모델 API 오류 (HTTP {exc.status_code}). 잠시 후 다시 시도해 주세요.") from exc

    latency = time.time() - started

    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMError("안전 정책에 의해 이 요청은 처리되지 않았습니다. 질문을 바꿔서 시도해 주세요.")

    text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return LLMResponse(text=text, usage=_extract_usage(resp, latency),
                       stop_reason=getattr(resp, "stop_reason", None),
                       raw_blocks=list(resp.content))


def stream_text(client: anthropic.Anthropic, *, model: str, system: str,
                messages: list[dict], max_tokens: int = 1500,
                effort: str = "low"):
    """자연어 요약을 토큰 단위로 흘려보낸다. 마지막에 Usage 를 yield 한다."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if effort:
        kwargs["output_config"] = {"effort": effort}
    started = time.time()
    try:
        with client.messages.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                yield chunk
            final = stream.get_final_message()
        yield _extract_usage(final, time.time() - started)
    except anthropic.APIStatusError as exc:
        raise LLMError(f"요약 생성 중 API 오류 (HTTP {exc.status_code}).") from exc
    except anthropic.APIError as exc:
        raise LLMError(f"요약 생성에 실패했습니다: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------
# 태그 파싱
# --------------------------------------------------------------------------
def extract_tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    # 닫는 태그가 잘린 경우까지 구제
    m = re.search(rf"<{tag}>(.*)", text, re.S | re.I)
    return m.group(1).strip() if m else None


def parse_sql_response(text: str) -> dict:
    """모델 출력에서 sql / reasoning / assumption / clarify 를 뽑는다."""
    sql = extract_tag(text, "sql")
    if not sql:
        # 태그를 놓친 경우 코드펜스라도 찾는다 (형식 이탈 내성)
        fence = re.search(r"```sql\s*(.+?)```", text, re.S | re.I)
        sql = fence.group(1).strip() if fence else None
    return {
        "sql": sql,
        "reasoning": extract_tag(text, "reasoning"),
        "assumption": extract_tag(text, "assumption"),
        "clarify": extract_tag(text, "clarify"),
        "raw": text,
    }
