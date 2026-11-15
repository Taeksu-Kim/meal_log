"""python -m meal_log serve [--db data/meals.db] [--port 8766] [--card-url http://127.0.0.1:8765]

이 PC 안에서만 열린다 (127.0.0.1 고정). Windows 브라우저에서도 http://127.0.0.1:<port>
("localhost"는 IPv6로 먼저 가서 시간 초과가 날 수 있음 -- card_spending에서 측정).
"""

from __future__ import annotations

import argparse
from pathlib import Path

HOST = "127.0.0.1"  # 고정: 개인 식단·카드 기록 -- 네트워크에 열지 않는다 (CLAUDE.md)
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "meals.db"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="meal_log")
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="식단 캘린더 화면 서버")
    serve.add_argument("--db", default=str(DEFAULT_DB))
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--card-url", default="http://127.0.0.1:8765",
                       help="card_spending 서버 (이 PC 안). 다른 기기 주소를 넣지 말 것")
    serve.add_argument("--llm-url", default="http://127.0.0.1:8002/v1",
                       help="로컬 vLLM (채팅 해석·kcal 추정, 문장·메뉴 이름만 보냄). 클라우드 주소를 넣지 말 것")
    serve.add_argument("--ocr-url", default="http://127.0.0.1:8090",
                       help="OCR 서버 (../doc_reader/doc_parser/tools/ocr.sh start). 영수증·캡처 읽기에만. 이 PC 안 주소만")
    args = ap.parse_args(argv)

    import uvicorn

    from meal_log.app import create_app
    from meal_log.sources import HttpCardSource

    print(f"DB: {args.db}\n열기: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(args.db, source=HttpCardSource(args.card_url), llm_url=args.llm_url,
                           ocr_url=args.ocr_url, self_url=f"http://{HOST}:{args.port}"), host=HOST, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
