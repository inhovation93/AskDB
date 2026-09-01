"""LLM 호출 계층 — OpenAI / Anthropic 두 공급자를 하나의 인터페이스로 감싼다.

파이프라인(`pipeline.py`)은 어떤 공급자를 쓰는지 전혀 모른다. 여기서만 분기한다.
그래서 나중에 공급자를 바꾸거나 추가해도 파이프라인·가드레일·평가 코드는 그대로다.

관심사:
  · 공급자 자동 판별 (API 키 접두어) 및 클라이언트 생성
  · 토큰 사용량·비용·지연시간 계측
  · 오류를 사용자에게 보여줄 수 있는 한국어 메시지로 변환
  · <sql> 같은 태그 파싱
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

OPENAI = "openai"
ANTHROPIC = "anthropic"

# 기본 모델. 계정에서 쓸 수 없는 모델이면 사이드바에서 바꿀 수 있고,
# '사용 가능한 모델 확인' 버튼으로 실제 목록을 조회할 수 있다.
DEFAULT_MODELS = {
    OPENAI: "gpt-5",
    ANTHROPIC: "claude-opus-5",
}
MODEL_SUGGESTIONS = {
    OPENAI: ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"],
    ANTHROPIC: ["claude-opus-5", "claude-sonnet-5"],
}

# 요금(USD / 1M tokens). 확실한 값만 등록하고, 없으면 비용 대신 토큰만 표시한다.
# (틀린 금액을 보여주는 것보다 표시하지 않는 편이 낫다)
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"in": 5.0, "out": 25.0},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0},
    "gpt-4o": {"in": 2.5, "out": 10.0},
    "gpt-4.1": {"in": 2.0, "out": 8.0},
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

# reasoning_effort 를 거부한 모델을 기억해 두고 다음 호출부터 빼고 보낸다.
_NO_EFFORT: set[str] = set()


class LLMError(Exception):
    """사용자에게 그대로 보여줘도 되는 한국어 오류."""


# ---------------------------------------------------------------------------
# 공급자 판별 / 클라이언트
# ---------------------------------------------------------------------------
def detect_provider(api_key: str) -> str:
    """API 키 접두어로 공급자를 판별한다. Anthropic 키만 'sk-ant-' 로 시작한다."""
    return ANTHROPIC if api_key.strip().startswith("sk-ant-") else OPENAI


def provider_label(provider: str) -> str:
    return "OpenAI" if provider == OPENAI else "Anthropic Claude"


def make_client(api_key: str, provider: str | None = None):
    """(client, provider) 를 반환한다."""
    provider = provider or detect_provider(api_key)
    if provider == ANTHROPIC:
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("anthropic 패키지가 설치되지 않았습니다. "
                           "requirements.txt 를 확인해 주세요.") from exc
        return anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2), provider
    try:
        import openai
    except ImportError as exc:
        raise LLMError("openai 패키지가 설치되지 않았습니다. "
                       "requirements.txt 를 확인해 주세요.") from exc
    return openai.OpenAI(api_key=api_key, timeout=90.0, max_retries=2), provider


def list_models(client, provider: str, limit: int = 40) -> list[str]:
    """계정에서 실제로 사용 가능한 모델 ID 목록 (설정 화면 도움용)."""
    try:
        if provider == OPENAI:
            names = [m.id for m in client.models.list()]
            chat = [n for n in names if n.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))]
            return sorted(chat or names)[:limit]
        return sorted(m.id for m in client.models.list())[:limit]
    except Exception as exc:
        raise LLMError(f"모델 목록을 불러오지 못했습니다: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# 사용량 / 응답
# ---------------------------------------------------------------------------
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

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write

    def cost_usd(self, model: str) -> float | None:
        """단가를 모르는 모델이면 None (추정 금액을 지어내지 않는다)."""
        p = PRICING.get(model)
        if p is None:
            return None
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


# ---------------------------------------------------------------------------
# 프롬프트 형식 변환
# ---------------------------------------------------------------------------
def _system_text(system: list[dict] | str) -> str:
    """블록 리스트로 조립된 system 을 단일 문자열로 합친다 (OpenAI 용)."""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system)


# ---------------------------------------------------------------------------
# 단일 호출
# ---------------------------------------------------------------------------
def complete(client, provider: str, *, model: str, system: list[dict] | str,
             messages: list[dict], max_tokens: int = 8000,
             effort: str = "medium", cache_system: bool = True) -> LLMResponse:
    if provider == ANTHROPIC:
        return _complete_anthropic(client, model=model, system=system, messages=messages,
                                   max_tokens=max_tokens, effort=effort,
                                   cache_system=cache_system)
    return _complete_openai(client, model=model, system=system, messages=messages,
                            effort=effort)


def _complete_openai(client, *, model: str, system, messages: list[dict],
                     effort: str) -> LLMResponse:
    import openai

    msgs = [{"role": "system", "content": _system_text(system)}] + messages
    # 토큰 상한은 일부러 보내지 않는다: 모델 세대별로 max_tokens /
    # max_completion_tokens 중 하나만 허용해 400 이 나기 쉽다.
    # 출력이 SQL 한 개라 길이는 프롬프트로 이미 제한된다.
    kwargs: dict = {"model": model, "messages": msgs}
    use_effort = effort and model not in _NO_EFFORT
    if use_effort:
        kwargs["reasoning_effort"] = effort

    started = time.time()
    try:
        resp = client.chat.completions.create(**kwargs)
    except openai.BadRequestError as exc:
        detail = str(exc)
        # 추론 모델이 아니면 reasoning_effort 를 거부한다 → 기억해 두고 재시도
        if use_effort and "reasoning_effort" in detail:
            _NO_EFFORT.add(model)
            kwargs.pop("reasoning_effort", None)
            try:
                resp = client.chat.completions.create(**kwargs)
            except openai.APIError as exc2:
                raise LLMError(_openai_message(exc2, model)) from exc2
        else:
            raise LLMError(_openai_message(exc, model)) from exc
    except openai.APIError as exc:
        raise LLMError(_openai_message(exc, model)) from exc

    latency = time.time() - started
    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "") if choice else ""
    finish = getattr(choice, "finish_reason", None) if choice else None

    u = getattr(resp, "usage", None)
    cached = 0
    if u is not None:
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
    usage = Usage(
        input_tokens=max((getattr(u, "prompt_tokens", 0) or 0) - cached, 0) if u else 0,
        output_tokens=(getattr(u, "completion_tokens", 0) or 0) if u else 0,
        cache_read=cached,
        calls=1,
        latency_sec=latency,
    )
    # OpenAI 의 finish_reason='length' 는 Anthropic 의 'max_tokens' 와 같은 의미다.
    stop = "max_tokens" if finish == "length" else (finish or None)
    return LLMResponse(text=text, usage=usage, stop_reason=stop)


def _openai_message(exc, model: str) -> str:
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return "OpenAI API 키가 유효하지 않습니다. 키를 다시 확인해 주세요."
    if isinstance(exc, openai.NotFoundError):
        return (f"'{model}' 모델을 사용할 수 없습니다. 사이드바에서 "
                "'사용 가능한 모델 확인'을 눌러 다른 모델을 선택해 주세요.")
    if isinstance(exc, openai.RateLimitError):
        return ("요청이 제한되었습니다(429). 사용 한도 또는 잔액을 확인하거나 "
                "잠시 후 다시 시도해 주세요.")
    if isinstance(exc, openai.APITimeoutError):
        return "모델 응답이 지연되어 시간 초과되었습니다. 다시 시도해 주세요."
    if isinstance(exc, openai.APIConnectionError):
        return "네트워크 연결에 실패했습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(exc, openai.BadRequestError):
        return f"요청이 거부되었습니다(400): {str(exc)[:200]}"
    status = getattr(exc, "status_code", "?")
    return f"모델 API 오류 (HTTP {status}). 잠시 후 다시 시도해 주세요."


def _complete_anthropic(client, *, model: str, system, messages: list[dict],
                        max_tokens: int, effort: str, cache_system: bool) -> LLMResponse:
    import anthropic

    if cache_system and isinstance(system, list) and system:
        system = [dict(b) for b in system]
        system[-1]["cache_control"] = {"type": "ephemeral"}

    kwargs: dict = {"model": model, "max_tokens": max_tokens,
                    "system": system, "messages": messages}
    if effort:
        kwargs["output_config"] = {"effort": effort}

    started = time.time()
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise LLMError("Anthropic API 키가 유효하지 않습니다.") from exc
    except anthropic.NotFoundError as exc:
        raise LLMError(f"'{model}' 모델을 사용할 수 없습니다.") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("요청이 몰려 잠시 제한되었습니다(429).") from exc
    except anthropic.APITimeoutError as exc:
        raise LLMError("모델 응답이 지연되어 시간 초과되었습니다.") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("네트워크 연결에 실패했습니다.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"모델 API 오류 (HTTP {exc.status_code}).") from exc

    latency = time.time() - started
    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMError("안전 정책에 의해 이 요청은 처리되지 않았습니다.")

    u = resp.usage
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        calls=1, latency_sec=latency,
    )
    text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return LLMResponse(text=text, usage=usage,
                       stop_reason=getattr(resp, "stop_reason", None),
                       raw_blocks=list(resp.content))


# ---------------------------------------------------------------------------
# 스트리밍 (자연어 요약용)
# ---------------------------------------------------------------------------
def stream_text(client, provider: str, *, model: str, system: str,
                messages: list[dict], max_tokens: int = 1200, effort: str = "low"):
    """텍스트 조각을 순차 yield 하고, 마지막에 Usage 를 1회 yield 한다."""
    if provider == ANTHROPIC:
        yield from _stream_anthropic(client, model=model, system=system,
                                     messages=messages, max_tokens=max_tokens,
                                     effort=effort)
    else:
        yield from _stream_openai(client, model=model, system=system, messages=messages)


def _stream_openai(client, *, model: str, system: str, messages: list[dict]):
    import openai

    msgs = [{"role": "system", "content": _system_text(system)}] + messages
    started = time.time()
    usage = Usage(calls=1)
    try:
        stream = client.chat.completions.create(
            model=model, messages=msgs, stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                u = chunk.usage
                details = getattr(u, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or 0
                usage.input_tokens = max((getattr(u, "prompt_tokens", 0) or 0) - cached, 0)
                usage.output_tokens = getattr(u, "completion_tokens", 0) or 0
                usage.cache_read = cached
            if chunk.choices:
                piece = chunk.choices[0].delta.content
                if piece:
                    yield piece
    except openai.APIError as exc:
        raise LLMError(_openai_message(exc, model)) from exc
    usage.latency_sec = time.time() - started
    yield usage


def _stream_anthropic(client, *, model: str, system: str, messages: list[dict],
                      max_tokens: int, effort: str):
    import anthropic

    kwargs: dict = {"model": model, "max_tokens": max_tokens,
                    "system": system, "messages": messages}
    if effort:
        kwargs["output_config"] = {"effort": effort}
    started = time.time()
    try:
        with client.messages.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                yield chunk
            final = stream.get_final_message()
    except anthropic.APIError as exc:
        raise LLMError(f"요약 생성에 실패했습니다: {type(exc).__name__}") from exc
    u = final.usage
    yield Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        calls=1, latency_sec=time.time() - started,
    )


# --------------------------------------------------------------------------
# 태그 파싱
# --------------------------------------------------------------------------
def extract_tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(rf"<{tag}>(.*)", text, re.S | re.I)  # 닫는 태그가 잘린 경우 구제
    return m.group(1).strip() if m else None


def parse_sql_response(text: str) -> dict:
    """모델 출력에서 sql / reasoning / assumption / clarify 를 뽑는다."""
    sql = extract_tag(text, "sql")
    if not sql:
        fence = re.search(r"```sql\s*(.+?)```", text, re.S | re.I)
        sql = fence.group(1).strip() if fence else None
    return {
        "sql": sql,
        "reasoning": extract_tag(text, "reasoning"),
        "assumption": extract_tag(text, "assumption"),
        "clarify": extract_tag(text, "clarify"),
        "raw": text,
    }
