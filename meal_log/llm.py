"""로컬 LLM 연결 -- meal_log에서 LLM을 부르는 곳(chat.py 채팅 해석, suggest.py kcal 추정)이 같이 쓰는 정책 하나.

- 로컬 vLLM만 (클라우드 주소를 넣지 말 것). thinking 끄기, JSON 스키마 고정, 답은 코드로 다시 검사 (실패하면 1번 재시도)
- 실패는 명시적 표식으로: 연결 안 됨 = ``LLMDown`` (API 503), 답이 검사를 통과 못 함 = ``LLMBadOutput`` (API 502)
- card_spending ``suggest.make_local_llm``과 같은 정책 -- 그쪽을 고칠 수 있게 되면 공용(basic_agent 등)으로 뺄 후보
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, TypeVar

T = TypeVar("T")


class LocalLLM(Protocol):
    """basic_agent의 LLMClient와 같은 모양 (OpenAICompatLLM·테스트용 가짜 둘 다 맞음)."""

    def complete(self, system: str, user: str, *, schema: Optional[dict] = None) -> str: ...


class LLMDown(Exception):
    """로컬 LLM에 연결할 수 없음."""


class LLMBadOutput(Exception):
    """LLM 답이 형식·범위 검사를 다시 시도해도 통과하지 못함."""


def make_local_llm(base_url: str, model: Optional[str] = None) -> LocalLLM:
    """model을 안 주면 서버의 /models 첫 번째 이름. 연결이 안 되면, basic_agent가 없으면 LLMDown (그 기능만 잠긴다)."""
    try:
        from agent.core.llm import OpenAICompatLLM
    except ImportError as exc:
        raise LLMDown("basic_agent가 없음 -- 이 저장소 옆에 git clone https://github.com/Taeksu-Kim/basic_agent "
                      "(또는 BASIC_AGENT_PATH)") from exc

    if model is None:
        import requests

        try:
            model = requests.get(f"{base_url.rstrip('/')}/models", timeout=5).json()["data"][0]["id"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMDown(f"로컬 LLM에 연결할 수 없음 ({base_url}): {type(exc).__name__}") from exc
    # Qwen3: thinking을 끄지 않으면 사고 토큰이 출력 예산을 먹어 JSON이 잘린다 (REF/LLM.md)
    return OpenAICompatLLM(base_url=base_url, model=model, timeout=60, max_retries=1,
                           sampling={"temperature": 0, "max_tokens": 800,
                                     "chat_template_kwargs": {"enable_thinking": False}})


def ask_json(llm: LocalLLM, system: str, user: str, schema: dict, check: Callable[[object], T], *, what: str) -> T:
    """스키마로 묻고 ``check(파싱한 JSON)``으로 검사. check는 문제가 있으면 ``agent.core.llm.OutputError``를 던진다."""
    from agent.core.llm import ContextOverflowError, LLMUnavailableError, OutputError, ask_parsed, extract_json

    try:
        return ask_parsed(llm, system, user, lambda s: check(extract_json(s, what=what)), schema=schema, retries=1, what=what)
    except LLMUnavailableError as exc:
        raise LLMDown(str(exc)) from exc
    except (OutputError, ContextOverflowError) as exc:
        raise LLMBadOutput(str(exc)) from exc
