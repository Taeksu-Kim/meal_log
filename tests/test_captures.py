"""영수증·캡처 올리기 테스트. 바깥(OCR 서버·card_spending)만 가짜, 이미지·글자·가게·금액은 전부 지어낸 것.
OCR 글자는 2026-11-15 지어낸 영수증 이미지를 실제 OCR 서버(PaddleOCR-VL)가 읽은 결과를 그대로 썼다 (수식 기호 섞임 포함)."""

from __future__ import annotations

import io
from datetime import date

from fastapi.testclient import TestClient
from PIL import Image

from doc_parser import OcrItem
from meal_log.app import create_app
from meal_log.receipts import parse_receipt
from tests.fake_card import FakeCard, row

DELIVERY = "\n".join(["영수증", "가나다치킨 역삼점", "주문일시 2026.10.10 19:12", "주문번호 B1X2Y3", "후라이드치킨 x 1\n18,000원",
                      "옵션: 순살 변경 +2,000원", "콜라  $ 1.25L \\times 1 $ 2,500원", "배달팁 3,000원", "할인-5,500원", "결제금액 20,000원"])
PAPER = "라마바 편의점 선릉점\n2026-10-11 15:03\n삼각김밥 참치마요 1,500\n바나나우유 240ml 1,700\n새우깡 90g 1,800\n합계 5,000\n신용카드 5,000"
TEXT_BY_WIDTH = {301: DELIVERY, 302: PAPER, 303: "흐릿해서 읽은 글자 없음"}


class FakeOcr:
    name = "fake"

    def __init__(self) -> None:
        self.reads = 0

    def check(self) -> None:
        pass

    def read(self, png: bytes, mode: str) -> list[OcrItem]:
        self.reads += 1
        w = Image.open(io.BytesIO(png)).width
        return [OcrItem(type="text", bbox=(0, 0, 1, 1), text=TEXT_BY_WIDTH[w])]


def img(width: int, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, 40), (width % 256, 10, 10)).save(buf, fmt)
    return buf.getvalue()


ROWS = [row("2026-10-10", "19:30", "우아한형제들", 20000, "배달"),     # 금액·날짜 맞음 (시각 18분 차이)
        row("2026-10-11", "12:00", "쿠팡이츠", 20000, "배달"),         # 같은 금액, 하루 뒤 (배달은 ±1일이라 후보)
        row("2026-10-13", "19:00", "우아한형제들", 20000, "배달"),     # 같은 금액이지만 3일 뒤 -> 후보 아님
        row("2026-10-10", "19:20", "다른가게", 21000, "배달"),         # 금액 다름
        row("2026-10-11", "15:05", "라마바편의점", 5000, "편의점")]


def make(tmp_path, ocr=None, rows=ROWS):
    fake = FakeCard(list(rows))
    c = TestClient(create_app(tmp_path / "data" / "m.db", source=fake.source, ocr=ocr))
    c.post("/api/sync")
    return c


def upload(c, *blobs):
    return c.post("/api/captures", files=[("files", (f"c{i}.png", b, "image/png")) for i, b in enumerate(blobs)])


# ---------------------------------------------------------------- 글자 -> 영수증 (규칙)
def test_parse_delivery_and_paper():
    d = parse_receipt(DELIVERY, date(2026, 11, 15))
    assert (d.kind, d.store, d.day, d.time, d.total) == ("delivery", "가나다치킨 역삼점", date(2026, 10, 10), "19:12", 20000)
    assert [(i.name, i.qty, i.price) for i in d.items] == [("후라이드치킨", 1, 18000), ("콜라 1.25L", 1, 2500)]   # 옵션·배달팁·할인 뺌
    p = parse_receipt(PAPER, date(2026, 11, 15))
    assert (p.kind, p.store, p.total) == ("paper", "라마바 편의점 선릉점", 5000)
    assert [i.name for i in p.items] == ["삼각김밥 참치마요", "바나나우유 240ml", "새우깡 90g"]


def test_parse_variants():
    r = parse_receipt("배달의민족\n마라공방\n10월 3일 오후 7:05 주문\n마라탕 2개 24,000원\n꿔바로우\n15,000원\n총 결제금액 39,000원",
                      date(2026, 11, 15))
    assert (r.kind, r.day, r.time, r.total) == ("delivery", date(2026, 10, 3), "19:05", 39000)
    assert [(i.name, i.qty) for i in r.items] == [("마라탕", 2), ("꿔바로우", 1)]
    assert parse_receipt("12월 30일 점심\n김밥 3,000", date(2026, 11, 15)).day == date(2025, 12, 30)   # 미래 날짜는 작년
    multi = parse_receipt("가게\n2026.10.1 12:00\n김밥 x 2 8,000\n즉시할인 1,000원\n합계 8,000\n쿠폰 할인 -1,000\n최종 결제금액 7,000원")
    assert multi.total == 7000 and [(i.name, i.qty) for i in multi.items] == [("김밥", 2)]   # 금액 줄이 여럿이면 마지막(최종)
    nothing = parse_receipt("흐릿해서 읽은 글자 없음")
    assert nothing.total is None and nothing.items == [] and nothing.day is None and nothing.store is None


# ---------------------------------------------------------------- 올리기·짝짓기·확정
def test_upload_reads_and_matches_card_payment(tmp_path):
    ocr = FakeOcr()
    c = make(tmp_path, ocr)
    r = upload(c, img(301), img(302, "JPEG")).json()
    d, p = r["captures"]
    assert r["errors"] == [] and ocr.reads == 2
    assert (d["store"], d["date"], d["time"], d["total"], d["kind"]) == ("가나다치킨 역삼점", "2026-10-10", "19:12", 20000, "delivery")
    assert [x["place"] for x in d["candidates"]] == ["우아한형제들", "쿠팡이츠"]   # 같은 날 먼저, 3일 뒤·다른 금액은 아님
    assert [x["place"] for x in p["candidates"]] == ["라마바편의점"]               # 종이 영수증은 같은 날만
    assert d["items"][0]["name"] == "후라이드치킨" and d["items"][0]["known"] is False
    assert (tmp_path / "data" / "captures" / f"{d['id']}.png").is_file()          # 이미지는 data/ 아래 (git 제외)
    assert c.get(f"/api/captures/{d['id']}/image").status_code == 200
    assert c.get("/api/captures/../m.db/image").status_code == 404
    assert c.get("/api/captures/0123456789abcdef01234567/image").status_code == 404


def test_same_image_twice_not_read_again(tmp_path):
    ocr = FakeOcr()
    c = make(tmp_path, ocr)
    upload(c, img(301))
    again = upload(c, img(301)).json()["captures"][0]
    assert again["duplicate"] and ocr.reads == 1
    assert len(c.get("/api/captures").json()) == 1


def test_bad_files_and_ocr_down(tmp_path):
    c = make(tmp_path, FakeOcr())
    r = c.post("/api/captures", files=[("files", ("a.txt", b"not an image", "text/plain")),
                                       ("files", ("big.png", b"0" * (15 * 1024 * 1024 + 1), "image/png"))]).json()
    assert r["captures"] == [] and [e["filename"] for e in r["errors"]] == ["a.txt", "big.png"]
    assert "이미지 파일이 아님" in r["errors"][0]["message"] and "너무 큼" in r["errors"][1]["message"]
    down = make(tmp_path / "d")                                                    # OCR도 주소도 없음
    r = upload(down, img(301))
    assert r.status_code == 503 and r.json()["detail"]["kind"] == "ocr_down"
    assert down.get("/api/captures").json() == []                                  # 못 읽은 건 저장하지 않음


def test_confirm_into_card_meal_then_undo(tmp_path):
    c = make(tmp_path, FakeOcr())
    d = upload(c, img(301)).json()["captures"][0]
    target = d["candidates"][0]["id"]
    r = c.post(f"/api/captures/{d['id']}/confirm", json={"target": str(target), "store": d["store"],
                                                         "items": [{"name": "치킨", "qty": 1}, {"name": "콜라 1.25L", "qty": 1}]}).json()
    assert r["meal_id"] == target and r["previous"] == {"items": [], "store": None, "confirmed": False}
    m = next(x for x in c.get("/api/meals").json() if x["id"] == target)
    assert [i["name"] for i in m["items"]] == ["치킨", "콜라 1.25L"] and m["store"] == "가나다치킨 역삼점" and m["kcal"]["partial"]
    assert c.get("/api/captures").json() == [] and len(c.get("/api/captures", params={"status": "confirmed"}).json()) == 1
    # 다시 동기화해도 가게 이름·메뉴 유지 (card_spending 값이 아님)
    c.post("/api/sync")
    m = next(x for x in c.get("/api/meals").json() if x["id"] == target)
    assert m["store"] == "가나다치킨 역삼점" and len(m["items"]) == 2
    # 되돌리기: 화면은 이전 값으로 끼니를 되돌리고 캡처를 확인 대기로
    c.put(f"/api/meals/{target}", json={"items": [], "store": None, "confirmed": False})
    m = next(x for x in c.get("/api/meals").json() if x["id"] == target)
    assert m["items"] == [] and m["store"] is None                      # 가게 이름도 되돌아감
    c.put(f"/api/captures/{d['id']}/status", json={"status": "read"})
    back = c.get("/api/captures").json()[0]
    assert back["candidates"][0]["id"] == target and back["meal"] is None


def test_confirm_new_meal_and_dismiss(tmp_path):
    c = make(tmp_path, FakeOcr(), rows=[])
    p = upload(c, img(302)).json()["captures"][0]
    assert p["candidates"] == [] and p["guess_slot"] == "간식"      # 15:03 -> 간식 시간대
    assert c.post(f"/api/captures/{p['id']}/confirm", json={"target": "new", "items": [{"name": "삼각김밥"}]}).status_code == 400
    r = c.post(f"/api/captures/{p['id']}/confirm", json={"target": "new", "date": p["date"], "slot": "간식", "store": p["store"],
                                                         "items": [{"name": "삼각김밥"}, {"name": "바나나우유 240ml"}]}).json()
    m = next(x for x in c.get("/api/meals").json() if x["id"] == r["meal_id"])
    assert (m["date"], m["slot"], m["place"], m["note"]) == ("2026-10-11", "간식", "라마바 편의점 선릉점", "영수증")
    assert c.post(f"/api/captures/{p['id']}/confirm", json={"target": "99999", "items": [{"name": "김밥"}]}).status_code == 404
    q = upload(c, img(303)).json()["captures"][0]
    assert q["items"] == [] and q["total"] is None                    # 못 읽은 캡처도 목록에 (사람이 볼 수 있게)
    assert c.put(f"/api/captures/{q['id']}/status", json={"status": "dismissed"}).status_code == 200
    assert [x["id"] for x in c.get("/api/captures").json()] == []
    assert c.put("/api/captures/nope/status", json={"status": "read"}).status_code == 404


# ---------------------------------------------------------------- 실제 OCR 결과와 같은 구조 (값은 지어냄)
# 2026-11-15 실제 사진 2장에서 본 OCR 버릇: 표 칸 안 줄바꿈이 글자 "\n", "ㄴ"->"L", "₩"->"#", 가려진 "11월"->"1월", 매장명은 맨 아래
def table(rows):
    return OcrItem(type="table", bbox=(0, 0, 1, 1), rows=rows)


def text(t):
    return OcrItem(type="text", bbox=(0, 0, 1, 1), text=t)


PAPER_DELIVERY = [
    OcrItem(type="heading", bbox=(0, 0, 1, 1), text="배민1"), text("결제수단:완불"), text("주문번호 : ABCD"),
    text("1월 12일 (목) 오후 7:42"), text("배달요청사항:문 앞\n고객연락처:0000-1111-2222"),
    table([["메뉴명", "수량", "금액"],
           ["[대표]양념곱창\\nL 기본3찬+쌈\\nL M사이즈 (기본)\\nL 리뷰 이벤트) 괜찮아요\\nL 순대 추가\\nL 콜라 500ml", "1", "# 15,000\\n3,000"],
           ["주문금액\\n할인금액\\n결제금액", "1,000", "# 19,000\\n-# 3,000\\n# 16,000"], ["매장명 :", "막창", ""]]),
    text("매장명 :가나곱창,막창")]
LABEL = [text("가나다불고기"), text("*식품의유형:양념육"), text("*중 량:200 g"), text("*소비기한:2026.11.20까지"),
         table([["0", "영양정보", ""], ["", "총 내용량", "200g 410kcal"], ["", "나트륨", "700mg 35%"]])]


def test_table_and_paper_delivery_quirks():
    from meal_log.receipts import ocr_to_text
    r = parse_receipt(ocr_to_text(PAPER_DELIVERY), date(2026, 11, 15))
    assert (r.kind, r.store, r.total, r.time) == ("delivery", "가나곱창,막창", 16000, "19:42")     # 짝지은 결제금액, 긴 매장명
    assert r.day == date(2026, 11, 12) and r.fixed_date                                          # 1/12는 월요일 -> 목요일인 11/12
    assert [(i.name, i.qty, i.option) for i in r.items] == [("양념곱창", 1, False), ("순대 추가", 1, True), ("콜라 500ml", 1, True)]
    assert r.items[0].read_name == "[대표]양념곱창" and r.items[0].price == 15000
    assert not any("0000" in i.name or "연락처" in i.name for i in r.items)                    # 전화번호는 품목이 아님


def test_phone_number_is_not_price():
    r = parse_receipt("가게\n2026.10.1 12:00\n김밥 3,000\n문의 02-555-1234\n주문코드 12345678\n합계 3,000")
    assert [i.name for i in r.items] == ["김밥"]          # 전화번호 끝자리·긴 숫자(바코드·주문번호)의 뒷부분이 가격으로 읽히지 않게


def test_weekday_fix_only_when_unambiguous():
    plain = parse_receipt("가게\n3월 5일 점심\n김밥 3,000", date(2026, 11, 15))
    assert plain.day == date(2026, 3, 5) and not plain.fixed_date                                 # 요일 없으면 그대로
    ok = parse_receipt("가게\n3월 5일 (목) 12:00\n김밥 3,000", date(2026, 11, 15))
    assert ok.day == date(2026, 3, 5) and not ok.fixed_date                                       # 요일이 맞으면 그대로


def test_label_parse_and_capture(tmp_path):
    from meal_log.receipts import ocr_to_text
    r = parse_receipt(ocr_to_text(LABEL))
    assert r.kind == "label" and (r.label.name, r.label.weight, r.label.kcal) == ("가나다불고기", 200, 410)
    per = parse_receipt("라라쿠키\n영양정보\n총 내용량 120g\n30g당 150kcal")
    assert per.label.kcal == 600 and per.label.weight == 120                                     # 30g당 -> 총 내용량으로 환산

    class LabelOcr(FakeOcr):
        def read(self, png, mode):
            return LABEL
    c = make(tmp_path, LabelOcr(), rows=[])
    v = upload(c, img(305)).json()["captures"][0]
    assert v["kind"] == "label" and v["label"] == {"name": "가나다불고기", "weight": 200, "unit": "g", "kcal": 410, "exists": False}
    assert v["candidates"] == []
    # 화면: 내 메뉴로 저장(포장지 영양정보) + 캡처는 반영함
    assert c.put("/api/menus/가나다불고기", json={"unit": "1팩(200g)", "low": 410, "mid": 410, "high": 410, "source": "label"}).status_code == 200
    assert c.put(f"/api/captures/{v['id']}/status", json={"status": "confirmed"}).status_code == 200
    item = c.post("/api/chat/parse", json={"text": "저녁에 불고기 가나다랑 밥", "today": "2026-11-15"}).json()
    assert item["entries"][0]["items"][0]["name"] in ("가나다불고기", "불고기 가나다")


def test_similar_menus_for_unknown_item(tmp_path):
    c = make(tmp_path, FakeOcr(), rows=[])
    sims = c.get("/api/menus/similar", params={"name": "직화막창"}).json()
    assert sims[0]["name"] == "곱창"                                   # 가게 이름 규칙(곱창|막창)으로
    assert c.get("/api/menus/similar", params={"name": "쀍쉙"}).json() == []


# ---------------------------------------------------------------- 배달앱 "주문상세" 화면 (날짜·결제금액이 잘린 캡처)
# 실제 배민 주문상세 캡처(2026-11-15)와 같은 구조에 지어낸 가게·메뉴·금액. OCR이 "배달팁"을 "배달팀"으로 읽은 것도 그대로.
DETAIL = ("주문상세\n가나분식 역삼점 >\n주문 메뉴\n결제 정보\n배달정보\n주문 메뉴\n"
          "[사장님추천]치즈 떡볶이(2인분)\n·가격: (14,000원)\n·맵기 선택: 보통맛\n·리뷰 약속 선택: 리뷰는 다음에 (무료는 1개만)\n"
          "·[추가선택] 사이드 선택: 김말이튀김 (3,000원)\n·[추가선택] 사리 선택: 라면사리 추가 안함 (0원)\n17,000원 1개\n"
          "리뷰 쓰기 (D-3)\n같은 메뉴 담기\n바로 주문\n순대\n·가격: (4,000원)\n8,000원 2개\n"
          "결제 정보\n메뉴금액\n배달팀\n[그림] 메뉴금액 / 25,000원 / 배달팁 / 2,000원")
DETAIL_DISCOUNT = DETAIL.replace("배달팁 / 2,000원", "배달팁 / 2,000원 / 할인 / -3,000원")
DETAIL_NO_MONEY = DETAIL.split("결제 정보\n메뉴금액")[0]
DETAIL_ROWS = [row("2026-11-01", "19:00", "우아한형제들", 27000, "배달"),
               row("2026-11-05", "19:00", "쿠팡이츠", 27000, "배달"),
               row("2026-11-07", "19:00", "우아한형제들", 27000, "배달"),    # 메뉴를 넣어 둠 -> 같은 금액 중 뒤로
               row("2026-11-08", "19:00", "우아한형제들", 27000, "배달"),
               row("2026-11-03", "12:00", "우아한형제들", 24000, "배달"),    # 할인 3천 원이면 맞음
               row("2026-11-04", "12:00", "우아한형제들", 21000, "배달"),    # 6천 원 차이 -> 아님
               row("2026-11-06", "12:00", "다라식당", 27000, "외식")]         # 배달 아님 -> 아님


class DetailOcr(FakeOcr):
    def read(self, png, mode):
        self.reads += 1
        w = Image.open(io.BytesIO(png)).width
        return [text({311: DETAIL, 312: DETAIL_DISCOUNT, 313: DETAIL_NO_MONEY}[w])]


def test_parse_order_detail_screen():
    r = parse_receipt(DETAIL, date(2026, 11, 15))
    assert (r.kind, r.store, r.day, r.time, r.total) == ("delivery", "가나분식 역삼점", None, None, None)
    assert [(i.name, i.qty, i.price, i.option) for i in r.items] == [
        ("치즈 떡볶이(2인분)", 1.0, 14000, False), ("김말이튀김", 1.0, 3000, True), ("순대", 2.0, 4000, False)]
    assert (r.menu_total, r.tip, r.discount, r.total_guess) == (25000, 2000, None, 27000)
    d = parse_receipt(DETAIL_DISCOUNT, date(2026, 11, 15))
    assert (d.discount, d.total_guess) == (3000, 24000)
    n = parse_receipt(DETAIL_NO_MONEY, date(2026, 11, 15))
    assert n.total_guess is None and [i.name for i in n.items] == ["치즈 떡볶이(2인분)", "김말이튀김", "순대"]
    # 결제금액이 보이면 그게 우선 (추정 안 씀)
    t = parse_receipt(DETAIL + "\n결제금액 26,000원", date(2026, 11, 15))
    assert t.total == 26000 and t.total_guess is None


def test_undated_detail_matches_by_amount_then_teaches_memory(tmp_path):
    c = make(tmp_path, DetailOcr(), rows=DETAIL_ROWS)
    m7 = next(m for m in c.get("/api/meals").json() if m["date"] == "2026-11-07")
    c.put(f"/api/meals/{m7['id']}", json={"items": [{"name": "치킨", "qty": 1}]})
    d = upload(c, img(311)).json()["captures"][0]
    assert d["date"] is None and d["total"] is None and d["total_guess"] == 27000 and (d["menu_total"], d["tip"]) == (25000, 2000)
    got = [(x["date"], x["exact"]) for x in d["candidates"]]
    # 같은 금액·메뉴 모름(최근 순) -> 같은 금액·메뉴 있음 -> 할인됐을 수 있는 금액. 외식·6천 원 차이는 없음
    assert got == [("2026-11-08", True), ("2026-11-05", True), ("2026-11-01", True), ("2026-11-07", True), ("2026-11-03", False)]
    # 확정 -> 그 결제에 메뉴·가게 -> 같은 앱·같은 금액인 다른 끼니에 경험 기억 제안
    target = next(x["id"] for x in d["candidates"] if x["date"] == "2026-11-01")
    items = [{"name": "떡볶이", "qty": 1}, {"name": "순대", "qty": 2}]
    assert c.post(f"/api/captures/{d['id']}/confirm", json={"target": str(target), "store": d["store"], "items": items}).status_code == 200
    m8 = next(m for m in c.get("/api/meals").json() if m["date"] == "2026-11-08")
    sg = {tuple(i["name"] for i in x["items"]): x for x in m8["suggest"]}   # 11/7 치킨도 같은 금액 기억 -> 최근 것 먼저
    assert list(sg) == [("치킨",), ("떡볶이", "순대")] and sg[("떡볶이", "순대")]["store"] == "가나분식 역삼점"
    m5 = next(m for m in c.get("/api/meals").json() if m["date"] == "2026-11-05")
    assert all(s["items"] != items for s in m5["suggest"])               # 다른 앱(쿠팡이츠)엔 안 퍼짐


def test_undated_detail_with_discount_or_without_money(tmp_path):
    c = make(tmp_path, DetailOcr(), rows=DETAIL_ROWS)
    d = upload(c, img(312)).json()["captures"][0]
    assert d["total_guess"] == 24000 and [(x["date"], x["exact"]) for x in d["candidates"]] == [("2026-11-03", True)]  # 할인을 봤으니 근사 안 함
    n = upload(c, img(313)).json()["captures"][0]
    assert n["total_guess"] is None and n["candidates"] == [] and n["store"] == "가나분식 역삼점"
