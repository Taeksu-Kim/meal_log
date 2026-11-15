"""영수증·캡처·영양정보 라벨 읽기 정확도 -- 실제 사진 + 정답 (data/eval_captures/, git 제외).

    python tools/capture_eval.py [--ocr-url http://127.0.0.1:8090]

정답(truth.json)은 사진을 보고 사람이 먼저 적는다. 필드별로 맞음/틀림을 센다 (틀리면 기대값과 읽은 값을 같이 보여 줌).
규칙을 고친 뒤 같은 사진으로 다시 재면 과적합 -- 새 사진을 더해 확인한다.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageOps  # noqa: E402

from meal_log import receipts  # noqa: E402

EVAL = ROOT / "data" / "eval_captures"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr-url", default="http://127.0.0.1:8090")
    ap.add_argument("--today", default=None, help="날짜 해석 기준일 (기본: 오늘)")
    args = ap.parse_args()
    from doc_parser import HttpOcr

    ocr = HttpOcr(args.ocr_url, timeout=300)
    ocr.check()
    today = date.fromisoformat(args.today) if args.today else date.today()
    truth = {k: v for k, v in json.loads((EVAL / "truth.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
    ok = total = 0
    for fname, want in truth.items():
        im = ImageOps.exif_transpose(Image.open(EVAL / fname)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        t = time.monotonic()
        r = receipts.parse_receipt(receipts.ocr_to_text(ocr.read(buf.getvalue(), "page")), today)
        dt = time.monotonic() - t
        got = {"kind": r.kind}
        if r.kind == "label" and r.label:
            got |= {"name": r.label.name, "weight_g": r.label.weight, "kcal": r.label.kcal}
        else:
            got |= {"store": r.store, "date": r.day.isoformat() if r.day else None, "time": r.time, "total": r.total,
                    "main_items": [i.name for i in r.items if not i.option],
                    "food_options": [i.name for i in r.items if i.option], "menu_total": r.menu_total, "tip": r.tip}
        print(f"== {fname} ({dt:.1f}s)" + (" · 요일로 날짜 바로잡음" if r.fixed_date else ""))
        for k, v in want.items():
            g = got.get(k)
            hit = (g == v) if not isinstance(v, (int, float)) or g is None else abs(g - v) < 0.5
            ok += hit
            total += 1
            print(f"  {'맞음' if hit else '틀림'} {k}" + ("" if hit else f": 기대 {v!r} / 읽음 {g!r}"))
        if r.kind != "label":
            print(f"  (참고) 곁들임 품목: {[i.name for i in r.items if i.option]}")
    print(f"\n필드 {ok}/{total} 맞음")


if __name__ == "__main__":
    main()
