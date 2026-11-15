"""영수증·배달앱 캡처·포장 식품 영양정보 라벨의 OCR 글자 -> 구조 (코드 규칙만, DESIGN.md 3.1절). 카드 결제와 짝짓기.

- 영수증: 가게·일시·결제금액·품목. "이름 ... 가격" 줄 = 품목, 배달팁·할인·합계 줄은 품목 아님. 옵션 줄("ㄴ"·"+"·"옵션")은
  돈을 낸 음식 추가만 곁들임 품목으로 (사이즈·기본 찬·이벤트·요청은 뺌). "결제금액·합계" = 결제금액 (여럿이면 마지막)
- 영양정보 라벨: 제품명·총 내용량·kcal ("총 내용량 200g 410kcal", "100g당 205kcal"). 포장 식품은 이게 가장 정확한 kcal
- OCR(PaddleOCR-VL) 버릇 (2026-11-15 실제 사진 2장·지어낸 영수증으로 실측): 수식 기호("$ \\times $"), 표 칸 안 줄바꿈이 글자 "\\n",
  "ㄴ"을 "L"로, "₩"을 "#"으로, 가려진 "11월"을 "1월"로 읽음 -> 요일 "(금)"이 있으면 그 요일과 맞는 날로 바로잡는다
- 화면은 항상 읽은 결과와 원문 글자를 보여 주고 사람이 고친 뒤 확정한다. 정확도는 tools/capture_eval.py (실제 사진 정답 셋)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

Kind = Literal["delivery", "paper", "label"]

PRICE = r"-?\d{1,3}(?:,\d{3})+|-?\d{3,7}"
_PRICE_TAIL = re.compile(rf"^(?P<name>.*?)\s*(?<![\d\-])(?P<price>{PRICE})\s*원?\s*$")
_ONLY_PRICE = re.compile(rf"^\s*\+?(?:{PRICE})\s*원?\s*$")
_TOTAL = re.compile(r"결제\s*금액|총\s*결제|최종\s*결제|받을\s*금액|합\s*계|총\s*액|총\s*금액")
_TOTAL_LABEL_ONLY = re.compile(r"^\s*(?:주문\s*금액|할인\s*금액|결제\s*금액|배달\s*팁|합\s*계|총\s*결제\s*금액|최종\s*결제\s*금액)\s*$")
# 품목이 아닌 줄 (금액이 붙어 있어도)
_NOT_ITEM = re.compile(r"배달\s*팁|배달\s*비|할인|쿠폰|포인트|적립|부가세|부가가치세|과세|면세|공급가|소계|합\s*계|총|결제|받을|"
                       r"거스름|현금|카드|승인|일시불|할부|주문\s*번호|주문\s*일시|사업자|대표|전화|연락처|TEL|주소|영수증|금액|"
                       r"단가|수량|봉투|회원|멤버십|잔액|변경|요청|매장명|상호", re.I)
_OPTION = re.compile(r"^\s*(?:옵션|\+|ㄴ|└|L\s|-\s|·)")
_OPTION_NOT_FOOD = re.compile(r"사이즈|기본|이벤트|리뷰|괜찮|요청|수저|포크|젓가락|맵기|단계|변경|선택|없음|안\s*함|추가\s*안|빼")
_DELIVERY = re.compile(r"배달|배민|쿠팡\s*이츠|요기요|땡겨요|주문\s*번호|가게\s*요청")
_LABEL = re.compile(r"영양\s*정보|영양\s*성분")
_STORE_LABEL = re.compile(r"(?:매장명|상호명?|가맹점명?|가게\s*이름|가게명)\s*[:：]?\s*(?P<name>.+)$")
_DATE = re.compile(r"(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일(?:\s*\(?\s*(?P<dow>[월화수목금토일])\s*\)?)?")
_TIME = re.compile(r"(오전|오후)?\s*(\d{1,2}):(\d{2})(?::\d{2})?")
_QTY = re.compile(r"\s*(?:[xX×*]\s*(\d{1,2})|(\d{1,2})\s*개|\s(\d{1,2}))\s*$")   # "x 2", "2개", 표의 수량 칸 " 1"
_HEADING = re.compile(r"^(?:영수증|주문\s*내역|주문\s*상세|결제\s*내역|카드\s*영수증|매출\s*전표|고객용)\s*$")
_TAG = re.compile(r"^\s*[\[(【][^\])】]{1,10}[\])】]\s*")   # "[시그니처]직화막창" -> 직화막창
DOW = "월화수목금토일"
# 영양정보 라벨
_LBL_TOTAL = re.compile(r"총\s*내용량\s*(?:당)?\s*(\d+(?:\.\d+)?)\s*(g|ml|mL)\s*(\d+(?:\.\d+)?)\s*kcal", re.I)
_LBL_PER = re.compile(r"(\d+(?:\.\d+)?)\s*(g|ml|mL)\s*당\s*(\d+(?:\.\d+)?)\s*kcal", re.I)
_LBL_WEIGHT = re.compile(r"(?:총\s*내용량|중\s*량|내\s*용\s*량)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(g|ml|mL|kg)", re.I)


@dataclass
class Item:
    name: str
    qty: float = 1.0
    price: Optional[int] = None
    option: bool = False        # 곁들임 (옵션 줄에서 온 돈 낸 음식 추가)
    read_name: str = ""         # OCR이 읽은 그대로 (꼬리표 떼기 전)


@dataclass
class Label:
    name: Optional[str]
    weight: Optional[float]      # 총 내용량 (g 또는 ml)
    unit: str                    # g / ml
    kcal: Optional[float]        # 총 내용량 전체의 kcal


@dataclass
class Receipt:
    kind: Kind
    store: Optional[str] = None
    day: Optional[date] = None
    time: Optional[str] = None          # "HH:MM"
    total: Optional[int] = None
    items: list[Item] = field(default_factory=list)
    label: Optional[Label] = None
    fixed_date: bool = False            # 요일로 날짜를 바로잡았음 (화면에 알림)
    menu_total: Optional[int] = None    # 메뉴금액 (배달앱 화면)
    tip: Optional[int] = None           # 배달팁
    discount: Optional[int] = None      # 할인 합 (양수)

    @property
    def total_guess(self) -> Optional[int]:
        """결제금액을 못 읽었을 때 짝짓기에 쓸 추정 = 메뉴금액 + 배달팁 - 할인 (할인 줄이 잘렸으면 실제는 이보다 적을 수 있음)."""
        if self.total is not None or self.menu_total is None:
            return None
        return self.menu_total + (self.tip or 0) - (self.discount or 0)


# ---------------------------------------------------------------- OCR 결과 -> 글자
def ocr_to_text(items) -> str:
    """doc_parser OcrItem 목록 -> 줄 글자. 표는 칸 구조대로 편다: 첫 칸과 끝 칸의 줄 수가 같으면 줄끼리 짝지음
    ("주문금액/할인금액/결제금액" ↔ "19,000/-3,000/16,000"), 아니면 첫 줄 + 가운데 칸(수량) + 끝 칸 첫 줄, 나머지 줄은 따로."""
    out: list[str] = []
    for it in items:
        if getattr(it, "type", "text") != "table" or not getattr(it, "rows", None):
            out.append(getattr(it, "text", "") or "")
            continue
        for row in it.rows:
            cells = [[ln.strip() for ln in _unescape(c).splitlines() if ln.strip()] for c in row]
            if not any(cells):
                continue
            first, last = cells[0], cells[-1] if len(cells) > 1 else []
            if len(cells) > 1 and len(first) > 1 and len(first) == len(last):
                out += [f"{a} {b}" for a, b in zip(first, last)]
                continue
            mids = [c[0] for c in cells[1:-1] if c]
            head = " ".join([first[0] if first else "", *mids, last[0] if last else ""]).strip()
            out.append(head)
            out += first[1:]
    return "\n".join(t for t in out if t)


def _unescape(s: str) -> str:
    return (s or "").replace("\\n", "\n")


def clean_ocr(text: str) -> str:
    """OCR 글자 정리: 수식 기호($, \\times 등)·글자 '\\n'·화폐 기호(₩, '#'로 읽힌 것) 정리, 공백 정리."""
    t = _unescape(text).replace("\\times", "x").replace("\\cdot", "·")
    t = re.sub(r"\$+", " ", t)
    t = re.sub(r"\\[a-zA-Z]+", " ", t)
    t = re.sub(r"[#₩￦]\s*(?=-?\s*\d)", "", t)          # "# 16,900" -> "16,900"
    t = re.sub(r"-\s*[#₩￦]\s*(?=\d)", "-", t)          # "-# 5,000" -> "-5,000"
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines() if ln.strip())


def _money(s: str) -> int:
    return int(s.replace(",", ""))


def _lines(text: str) -> list[str]:
    """이름 줄 다음에 가격만 있는 줄이 오면 한 줄로 합친다 ("후라이드치킨 x 1" / "18,000원")."""
    out: list[str] = []
    for ln in clean_ocr(text).splitlines():
        if out and _ONLY_PRICE.match(ln) and not _PRICE_TAIL.match(out[-1]) and not _TOTAL_LABEL_ONLY.match(out[-1]):
            out[-1] = f"{out[-1]} {ln.strip()}"
        else:
            out.append(ln)
    return out


# ---------------------------------------------------------------- 날짜
def _fix_by_weekday(month_read: int, day: int, dow: str, today: date) -> Optional[date]:
    """'1월 12일 (목)'처럼 앞자리가 가려져 달을 잘못 읽었을 때: 읽은 달로 끝나는 달(1 -> 1·11) 중 그 요일과 맞고
    오늘 이전 1년 안인 날. 하나로 정해질 때만."""
    want = DOW.index(dow)
    cands = []
    for back in range(0, 13):
        y, mo = today.year, today.month - back
        while mo <= 0:
            mo += 12
            y -= 1
        if str(mo).endswith(str(month_read)) or str(month_read).endswith(str(mo)):
            try:
                d = date(y, mo, day)
            except ValueError:
                continue
            if d <= today and d.weekday() == want and d not in cands:
                cands.append(d)
    return cands[0] if len(cands) == 1 else None


def _read_date(lines: list[str], today: date) -> tuple[Optional[date], bool]:
    for ln in lines:
        m = _DATE.search(ln)
        if m:
            try:
                return date(int(m[1]), int(m[2]), int(m[3])), False
            except ValueError:
                continue
        m = _DATE_MD.search(ln)
        if m:
            mo, dd = int(m[1]), int(m[2])
            d = None
            try:
                d = date(today.year, mo, dd)
                d = d if d <= today else d.replace(year=today.year - 1)
            except ValueError:
                pass
            if m["dow"] and (d is None or d.weekday() != DOW.index(m["dow"])):
                fixed = _fix_by_weekday(mo, dd, m["dow"], today)
                if fixed:
                    return fixed, True
            if d:
                return d, False
    return None, False


def _read_time(lines: list[str]) -> Optional[str]:
    for ln in lines:
        if not (_DATE.search(ln) or _DATE_MD.search(ln) or "시" in ln):
            continue
        m = _TIME.search(ln)
        if m:
            h = int(m[2]) + (12 if m[1] == "오후" and int(m[2]) < 12 else 0) - (12 if m[1] == "오전" and int(m[2]) == 12 else 0)
            if 0 <= h < 24:
                return f"{h:02d}:{m[3]}"
    return None


# ---------------------------------------------------------------- 영양정보 라벨
def _parse_label(lines: list[str]) -> Label:
    text = "\n".join(lines)
    name = None
    for ln in lines:                       # 제품명: 첫 한글 줄 ("*식품의 유형:" 같은 항목 줄·영양정보 표 줄 빼고)
        if ln.startswith(("*", "※", "[")) or re.search(r"[:：]", ln) or _LABEL.search(ln) or re.search(r"\d", ln):
            continue
        if re.search(r"[가-힣]{2,}", ln) and len(ln) <= 30:
            name = ln.strip()
            break
    weight = kcal = None
    unit = "g"
    m = _LBL_TOTAL.search(text)
    if m:
        weight, unit, kcal = float(m[1]), m[2].lower(), float(m[3])
    else:
        w = _LBL_WEIGHT.search(text)
        if w:
            weight, unit = float(w[1]) * (1000 if w[2].lower() == "kg" else 1), "g" if w[2].lower() == "kg" else w[2].lower()
        per = _LBL_PER.search(text)
        if per and weight:
            kcal = float(per[3]) * weight / float(per[1])
    return Label(name, weight, unit, kcal)


# ---------------------------------------------------------------- 영수증
def parse_receipt(text: str, today: Optional[date] = None) -> Receipt:
    today = today or date.today()
    lines = _lines(text)
    if _LABEL.search(text) and re.search(r"kcal", text, re.I):
        lab = _parse_label(lines)
        return Receipt(kind="label", store=None, label=lab)
    r = Receipt(kind="delivery" if _DELIVERY.search(text) else "paper")
    detail = bool(_DETAIL.search(text) and any(_BLOCK_END.match(ln) for ln in lines))
    if detail:
        r.kind = "delivery"
    _amount_parts(text, r)
    r.day, r.fixed_date = _read_date(lines, today)
    r.time = _read_time(lines)
    totals = [m for ln in lines if _TOTAL.search(ln) and (m := _PRICE_TAIL.match(ln))]
    if totals:                             # 할인 뒤의 최종 금액이 보통 마지막
        r.total = abs(_money(totals[-1]["price"]))
    labeled = [m["name"].strip(" :：") for ln in lines if (m := _STORE_LABEL.search(ln))]
    if labeled:                            # "매장명 : ○○" (표 조각에서 짧게 잘린 것보다 긴 쪽)
        r.store = max(labeled, key=len)
    else:                                  # 제목·일시·금액 줄이 아닌 첫 한글 줄
        for ln in lines:
            if _HEADING.match(ln) or _DATE.search(ln) or _TIME.search(ln) or _PRICE_TAIL.match(ln) or _NOT_ITEM.search(ln):
                continue
            if re.search(r"[가-힣]{2,}", ln) and len(ln) <= 30:
                r.store = re.sub(r"\s*[>›〉]\s*$", "", ln.strip())   # 배달앱 화면의 "가게 이름 >" 링크 표시
                break
    if detail:
        _parse_detail(lines, r)
    for ln in ([] if detail else lines):   # 품목: "이름 [x n] 가격" 줄 + 돈 낸 음식 옵션
        is_opt = bool(_OPTION.match(ln))
        body = _TAG.sub("", ln)            # "[대표메뉴]..." 같은 꼬리표가 '품목 아닌 줄' 단어(대표)에 걸리지 않게
        if _NOT_ITEM.search(body) or _DATE.search(ln) or _DATE_MD.search(ln) or (is_opt and _OPTION_NOT_FOOD.search(ln)):
            continue
        m = _PRICE_TAIL.match(ln)
        if m:
            name, price = m["name"].strip(), _money(m["price"])
            if price <= 0:
                continue
        elif is_opt and r.items:           # 값 없는 옵션 줄 (값은 OCR 표에서 다른 칸으로 흩어지곤 함)
            name, price = ln, None
        else:
            continue
        if is_opt:
            name = _OPTION.sub("", name).strip(" :：")
        if not re.search(r"[가-힣A-Za-z]", name):
            continue
        qty = 1.0
        q = _QTY.search(name)
        if q:
            qty = float(q[1] or q[2] or q[3])
            name = name[:q.start()].strip()
        read = name
        name = _TAG.sub("", name).strip()
        if name and name != r.store and 0 < qty <= 20:
            r.items.append(Item(name, qty, price, option=is_opt, read_name=read))
    if r.total is None and r.day is None and not r.items and r.menu_total is None:   # 영수증으로 읽힌 게 없으면 아무 글자나 가게 이름이 되지 않게
        r.store = None
    return r


# ---------------------------------------------------------------- 배달앱 "주문상세" 화면 (2026-11-15, 배민 실제 캡처 1장으로)
# 메뉴 덩어리 = 이름 줄 -> 옵션 줄들("· 라벨: 값 (6,500원)") -> "17,000원 1개"(덩어리 합계·수량). 가격이 괄호 안, 날짜·결제금액은
# 화면 아래쪽이라 잘리기 쉽다 -> 메뉴금액 + 배달팁으로 추정 금액 (Receipt.total_guess).
_DETAIL = re.compile(r"주문\s*상세")
_BLOCK_END = re.compile(rf"^(?P<price>{PRICE})\s*원\s*(?P<qty>\d{{1,2}})\s*개$")
_SECTION_ON = re.compile(r"^주문\s*메뉴$")
_SECTION_OFF = re.compile(r"^(?:결제|배달|주문)\s*정보$")
_BUTTON = re.compile(r"리뷰\s*쓰기|같은\s*메뉴\s*담기|바로\s*주문|다시\s*주문|장바구니|가게\s*보기")
_PAREN_PRICE = re.compile(rf"\(?\s*(?P<price>{PRICE})\s*원\s*\)?")
_OPTION_VALUE_NOT_FOOD = re.compile(r"사이즈|업그레이드|곱빼기|변경|맵기|단계|없음|안\s*함|빼|싫어|괜찮|다음에|기본")
_MENU_TOTAL = re.compile(rf"(?:메뉴|주문)\s*금액\D{{0,8}}?(?P<price>{PRICE})\s*원")
_TIP = re.compile(rf"배달\s*[팁팀]\D{{0,8}}?(?P<price>{PRICE})\s*원")     # OCR이 "배달팀"으로 읽음 (실측)
_DISCOUNT = re.compile(rf"할인[^\d\n]{{0,12}}?-\s*(?P<price>{PRICE})\s*원")


def _amount_parts(text: str, r: Receipt) -> None:
    """메뉴금액·배달팁·할인 (표 조각으로 흩어진 "메뉴금액 / 25,000원 / 배달팁 / 2,000원"도)."""
    flat = re.sub(r"\s*/\s*", " ", text)
    if m := _MENU_TOTAL.search(flat):
        r.menu_total = _money(m["price"])
    if m := _TIP.search(flat):
        r.tip = _money(m["price"])
    ds = [abs(_money(m["price"])) for m in _DISCOUNT.finditer(flat)]
    if ds:
        r.discount = sum(ds)


def _parse_detail(lines: list[str], r: Receipt) -> None:
    on, main = False, None
    for ln in lines:
        if _SECTION_ON.match(ln):
            on, main = True, None
            continue
        if _SECTION_OFF.match(ln):
            on = False
            continue
        if not on or _BUTTON.search(ln):
            continue
        if m := _BLOCK_END.match(ln):            # 덩어리 끝: 수량
            if main is not None:
                main.qty = float(m["qty"])
                main = None
            continue
        if _OPTION.match(ln):
            if main is None:
                continue
            body = _OPTION.sub("", ln).strip()
            label, _, value = body.rpartition(":")
            pm = _PAREN_PRICE.search(value)
            if not label.strip() or not pm:      # 값에 가격이 없으면 고른 옵션일 뿐 (맛·고수·리뷰 약속)
                continue
            if re.fullmatch(r"\s*가격\s*", label):
                main.price = _money(pm["price"])
                continue
            name = _PAREN_PRICE.sub("", value).strip(" :：")
            if name and _money(pm["price"]) > 0 and not _OPTION_VALUE_NOT_FOOD.search(name):
                r.items.append(Item(name, main.qty, _money(pm["price"]), option=True, read_name=body))
            continue
        if re.search(r"[가-힣A-Za-z]", ln) and not _ONLY_PRICE.match(ln) and not _NOT_ITEM.search(_TAG.sub("", ln)):
            main = Item(_TAG.sub("", ln).strip(), 1.0, None, read_name=ln)
            r.items.append(main)


# ---------------------------------------------------------------- 카드 결제와 짝짓기
def match_candidates(r: Receipt, meals: list[dict]) -> list[dict]:
    """금액이 정확히 같은 카드 결제 끼니. 날짜는 같은 날 (배달이면 ±1일: 늦은 밤 주문·청구 시차), 결제일 기준.
    가까운 순: 날짜 차이 -> 시각 차이(영수증에 시각이 있으면, ±15분 안을 우선)."""
    if r.kind == "label":
        return []
    if r.day is None:
        return _match_without_date(r, meals)
    if r.total is None:
        return []
    span = 1 if r.kind == "delivery" else 0
    out = []
    for m in meals:
        if m["source"] != "card" or m["amount"] != r.total or not m.get("pay_date"):
            continue
        dd = abs((date.fromisoformat(m["pay_date"]) - r.day).days)
        if dd > span:
            continue
        tdiff = None
        if r.time and m.get("time"):
            a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in (r.time, m["time"]))
            tdiff = min(abs(a - b), 1440 - abs(a - b))
        out.append((dd, 0 if tdiff is not None and tdiff <= 15 else 1, tdiff if tdiff is not None else 9999, m))
    out.sort(key=lambda x: x[:3])
    return [m for *_, m in out]


MAX_UNDATED = 8
DISCOUNT_SPAN = 5000   # 할인 줄이 잘렸을 수 있음 -> 추정 금액보다 이만큼 적은 결제까지 (쿠폰 1~5천 원)


def _match_without_date(r: Receipt, meals: list[dict]) -> list[dict]:
    """날짜를 못 읽은 배달앱 화면: 결제금액(없으면 추정 금액)이 같은 배달 결제, 메뉴 모르는 것·최근 것 먼저.
    추정 금액이고 할인 줄을 못 봤으면 그보다 조금 적은 결제도 뒤에 붙인다 (``exact`` 표시). 사람이 고른다."""
    want = r.total if r.total is not None else r.total_guess
    if want is None:
        return []
    loose = r.total is None and r.discount is None
    out = []
    for m in meals:
        if m["source"] != "card" or (r.kind == "delivery" and m.get("category") != "배달") or m["amount"] is None:
            continue
        exact = m["amount"] == want
        if not exact and not (loose and want - DISCOUNT_SPAN <= m["amount"] < want):
            continue
        out.append({**m, "exact": exact})
    out.sort(key=lambda m: (m["date"], m["time"] or ""), reverse=True)            # 최근 것 먼저
    out.sort(key=lambda m: (not m["exact"], bool(m.get("items"))))                  # 같은 금액·메뉴 모르는 것 먼저 (안정 정렬)
    return out[:MAX_UNDATED]


def date_window(r: Receipt) -> tuple[Optional[date], Optional[date]]:
    if r.day is None:
        return None, None
    span = timedelta(days=1 if r.kind == "delivery" else 0)
    return r.day - span, r.day + span
