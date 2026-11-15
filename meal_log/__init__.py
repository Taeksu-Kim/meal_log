"""meal_log -- 카드 결제·영수증·대화로 만드는 식단 캘린더 (로컬 전용). 설계는 DESIGN.md, 규칙은 CLAUDE.md.

basic_agent(github.com/Taeksu-Kim/basic_agent)를 ``agent`` 패키지로 import 한다 (로컬 LLM 호출에만 씀 -- 없으면 그 기능만 잠김).
기본은 이 저장소와 나란히 받은 ``../basic_agent`` 폴더, ``BASIC_AGENT_PATH``로 바꿀 수 있다.
"""

import os
import sys
from pathlib import Path

_BASIC_AGENT = os.environ.get("BASIC_AGENT_PATH", str(Path(__file__).resolve().parents[2] / "basic_agent"))
if _BASIC_AGENT not in sys.path:
    sys.path.insert(0, _BASIC_AGENT)
