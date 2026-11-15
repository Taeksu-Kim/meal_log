"""SQLite 저장소 (data/meals.db). 이 객체가 지키는 것: **사람이 고친 값은 동기화가 덮어쓰지 않는다** --
끼니를 옮겼으면(slot_locked) 날짜·끼니를, 메뉴를 고쳤으면(menu_user) 메뉴를 그대로 둔다.
연결 하나를 FastAPI 스레드풀이 같이 쓰므로 읽기도 잠금 안에서 한다 (card_spending에서 겪은 동시 읽기 결함과 같은 이유).

테이블
- meals:      끼니 기록 하나 = 카드 결제 하나(source='card', card_txn_id) 또는 직접 입력(source='manual').
              slot='장보기'는 먹은 끼니가 아니라 산 기록. skipped=끼니 건너뜀 표시, excluded=내가 먹은 게 아님(집계 제외)
- meal_items: 메뉴 (이름·수량·직접 넣은 kcal). kcal 기준표 값은 저장하지 않고 읽을 때 붙인다 (표를 고치면 바로 반영)
- settings:   끼니 시간대·간식 기준 금액·마지막 동기화 시각·정리 기준일
- user_menus: 기준표(menu_kcal.py)에 없는 메뉴의 kcal -- 사용자가 정한 값 또는 LLM 추정을 사용자가 확정한 값
- conversations·chat_messages: 대화 도우미 대화 (세션 종류·Claude 세션 id·메시지·도구가 한 일) -- 재시작 후 이어서
- a2a_tasks:  A2A로 들어온 일 (어떤 기능·상태·되물을 것·결과). 그 일이 만든 끼니·캡처는 화면에 그대로 보인다
- captures:   올린 영수증·캡처 (상태 read=확인 대기 / confirmed=끼니에 반영 / dismissed=치움). 이미지는 지우지 않는다 (치움은 되돌릴 수 있게)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from meal_log import menu_kcal, slots
from meal_log.sources import Fetched

SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, slot TEXT NOT NULL, time TEXT, place TEXT,
    source TEXT NOT NULL, card_txn_id TEXT UNIQUE, pay_date TEXT, category TEXT, amount INTEGER,
    people INTEGER NOT NULL DEFAULT 1, note TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
    slot_locked INTEGER NOT NULL DEFAULT 0, menu_user INTEGER NOT NULL DEFAULT 0, no_time INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_meals_date ON meals(date);
CREATE TABLE IF NOT EXISTS meal_items (
    id INTEGER PRIMARY KEY, meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    name TEXT NOT NULL, qty REAL NOT NULL DEFAULT 1, kcal_manual REAL, guessed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_items_meal ON meal_items(meal_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_menus (
    name TEXT PRIMARY KEY, unit TEXT NOT NULL, low INTEGER NOT NULL, mid INTEGER NOT NULL, high INTEGER NOT NULL,
    source TEXT NOT NULL, basis TEXT, updated_at TEXT NOT NULL);  -- 기준표에 없는 메뉴의 kcal (사용자가 정함 / LLM 추정을 확정)
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY, filename TEXT, created_at TEXT NOT NULL, ocr_text TEXT, parsed TEXT,
    status TEXT NOT NULL, meal_id INTEGER, error TEXT);  -- 올린 영수증·캡처 (id = 파일 내용 해시, 이미지는 data/captures/<id>.png)
CREATE TABLE IF NOT EXISTS memory_rejects (
    meal_id INTEGER NOT NULL, signature TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (meal_id, signature));   -- 경험 기억 제안 [틀림]: 이 끼니에 이 메뉴 묶음은 다시 제안하지 않음 (기억 자체는 저장 안 함)
CREATE TABLE IF NOT EXISTS a2a_tasks (
    id TEXT PRIMARY KEY, context_id TEXT NOT NULL, message_id TEXT UNIQUE, skill TEXT NOT NULL, state TEXT NOT NULL,
    request TEXT, pending TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    -- A2A로 들어온 일 (state: input_required·completed·failed·canceled·rejected). 재시작 후에도 GetTask·ListTasks
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, backend TEXT NOT NULL, title TEXT, claude_session TEXT, turns INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    claude_started INTEGER NOT NULL DEFAULT 0);   -- 대화 도우미 대화 (backend: claude | local). claude_started: 세션이 만들어짐 -> 다음은 --resume
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY, conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL,
    text TEXT NOT NULL, events TEXT, cost REAL, created_at TEXT NOT NULL);   -- role: user | assistant, events = 도구가 한 일
"""
# 나중에 생긴 열 (이미 있는 DB에 더한다)
MIGRATIONS = (("meals", "store", "ALTER TABLE meals ADD COLUMN store TEXT"),   # 영수증으로 알게 된 가게 이름 (배달앱 결제의 실제 가게)
              ("captures", "origin", "ALTER TABLE captures ADD COLUMN origin TEXT"))  # screen | a2a (어디로 들어온 캡처인지)


class NotFound(KeyError):
    pass


class NotAllowed(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            for table, col, sql in MIGRATIONS:
                if col not in {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}:
                    self._db.execute(sql)
        self.captures_dir = Path(path).parent / "captures" if str(path) != ":memory:" else None

    # ---------------------------------------------------------------- settings
    def setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: Optional[str]) -> None:
        with self._lock, self._db:
            if value is None:
                self._db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                self._db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (key, value))

    def slot_settings(self) -> slots.SlotSettings:
        raw = self.setting("slots")
        if not raw:
            return slots.SlotSettings()
        d = json.loads(raw)
        return slots.SlotSettings(windows={k: tuple(v) for k, v in d["windows"].items()},
                                  snack_meal_min_krw=int(d["snack_meal_min_krw"]))

    def set_slot_settings(self, s: slots.SlotSettings) -> int:
        """저장하고, 사람이 옮기지 않은 카드 끼니를 새 시간대로 다시 나눈다. 옮겨진 끼니 수."""
        with self._lock:
            self.set_setting("slots", json.dumps({"windows": s.windows, "snack_meal_min_krw": s.snack_meal_min_krw},
                                                 ensure_ascii=False))
            return self._reclassify(s)

    # ---------------------------------------------------------------- 동기화
    def _set_guess(self, meal_id: int, store_name: str) -> None:
        self._db.execute("DELETE FROM meal_items WHERE meal_id=?", (meal_id,))
        menu = menu_kcal.guess_menu(store_name)
        if menu:
            self._db.execute("INSERT INTO meal_items(meal_id, name, qty, guessed) VALUES (?, ?, 1, 1)", (meal_id, menu))

    def sync(self, fetched: Fetched) -> dict:
        """card_spending 식비 결제를 끼니로. 새 결제는 추가, 바뀐 결제는 갱신(사람이 고친 값은 유지),
        card_spending 기간 안인데 이제 없는 결제(취소됨)는 지운다. 같은 결과를 두 번 넣어도 그대로."""
        s = self.slot_settings()
        added = updated = unchanged = 0
        now = _now()
        with self._lock, self._db:
            have = {r["card_txn_id"]: r for r in self._db.execute("SELECT * FROM meals WHERE source='card'")}
            for t in fetched.txns:
                p, no_time = slots.place(t.pay_date, t.pay_time, t.minor, t.amount, s)
                old = have.get(t.txn_id)
                if old is None:
                    cur = self._db.execute(
                        "INSERT INTO meals(date, slot, time, place, source, card_txn_id, pay_date, category, amount, "
                        "no_time, updated_at) VALUES (?,?,?,?,'card',?,?,?,?,?,?)",
                        (p.date.isoformat(), p.slot, t.pay_time, t.name, t.txn_id, t.pay_date.isoformat(), t.minor,
                         t.amount, int(no_time), now))
                    self._set_guess(cur.lastrowid, t.name)
                    added += 1
                    continue
                new = {"time": t.pay_time, "place": t.name, "pay_date": t.pay_date.isoformat(), "category": t.minor,
                       "amount": t.amount, "no_time": int(no_time)}
                if not old["slot_locked"]:
                    new |= {"date": p.date.isoformat(), "slot": p.slot}
                diff = {k: v for k, v in new.items() if old[k] != v}
                if diff:
                    self._db.execute(f"UPDATE meals SET {', '.join(f'{k}=?' for k in diff)}, updated_at=? WHERE id=?",
                                     [*diff.values(), now, old["id"]])
                if not old["menu_user"] and ("place" in diff or not self._has_items(old["id"])):
                    self._set_guess(old["id"], t.name)
                updated += bool(diff)
                unchanged += not diff
            removed = 0
            if fetched.start and fetched.end:
                keep = {t.txn_id for t in fetched.txns}
                gone = [r["id"] for r in have.values() if r["card_txn_id"] not in keep
                        and fetched.start.isoformat() <= r["pay_date"] <= fetched.end.isoformat()]
                self._db.executemany("DELETE FROM meals WHERE id=?", [(i,) for i in gone])
                removed = len(gone)
            self._db.execute("INSERT INTO settings VALUES ('last_sync', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (now,))
        return {"fetched": len(fetched.txns), "added": added, "updated": updated, "unchanged": unchanged,
                "removed": removed, "period": {"start": fetched.start.isoformat() if fetched.start else None,
                                               "end": fetched.end.isoformat() if fetched.end else None}}

    def _has_items(self, meal_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM meal_items WHERE meal_id=? LIMIT 1", (meal_id,)).fetchone() is not None

    def _reclassify(self, s: slots.SlotSettings) -> int:
        moved = 0
        with self._lock, self._db:
            for r in self._db.execute("SELECT * FROM meals WHERE source='card' AND slot_locked=0").fetchall():
                p, no_time = slots.place(date.fromisoformat(r["pay_date"]), r["time"], r["category"], r["amount"], s)
                if (p.date.isoformat(), p.slot) != (r["date"], r["slot"]):
                    self._db.execute("UPDATE meals SET date=?, slot=?, no_time=?, updated_at=? WHERE id=?",
                                     (p.date.isoformat(), p.slot, int(no_time), _now(), r["id"]))
                    moved += 1
        return moved

    # ---------------------------------------------------------------- 읽기
    def meals(self, start: Optional[date] = None, end: Optional[date] = None) -> list[dict]:
        sql, args = "SELECT * FROM meals WHERE 1=1", []
        if start:
            sql += " AND date >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND date <= ?"
            args.append(end.isoformat())
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql + " ORDER BY date, COALESCE(time, '99'), id", args)]
            items: dict[int, list[dict]] = {}
            if rows:
                ids = [r["id"] for r in rows]
                for it in self._db.execute(
                        f"SELECT * FROM meal_items WHERE meal_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
                    items.setdefault(it["meal_id"], []).append(dict(it))
        for r in rows:
            r["items"] = items.get(r["id"], [])
        return rows

    def meal(self, meal_id: int) -> dict:
        with self._lock:
            row = self._db.execute("SELECT date FROM meals WHERE id=?", (meal_id,)).fetchone()
            if not row:
                raise NotFound(f"없는 끼니: {meal_id}")
            d = date.fromisoformat(row["date"])
            return next(m for m in self.meals(d, d) if m["id"] == meal_id)

    def last_sync(self) -> Optional[str]:
        return self.setting("last_sync")

    # ---------------------------------------------------------------- 사용자 메뉴
    def user_menus(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM user_menus ORDER BY name")]

    def user_menu(self, name: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM user_menus WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None

    def put_user_menu(self, name: str, *, unit: str, low: int, mid: int, high: int, source: str,
                      basis: Optional[str] = None) -> Optional[dict]:
        """저장(있으면 바꿈). 바꾸기 전 값을 돌려준다 (되돌리기용, 없었으면 None)."""
        with self._lock, self._db:
            prev = self.user_menu(name)
            self._db.execute(
                "INSERT INTO user_menus VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET unit=excluded.unit, "
                "low=excluded.low, mid=excluded.mid, high=excluded.high, source=excluded.source, basis=excluded.basis, "
                "updated_at=excluded.updated_at", (name, unit, low, mid, high, source, basis, _now()))
        return prev

    def delete_user_menu(self, name: str) -> dict:
        with self._lock, self._db:
            prev = self.user_menu(name)
            if prev is None:
                raise NotFound(f"내 메뉴에 없음: {name}")
            self._db.execute("DELETE FROM user_menus WHERE name=?", (name,))
        return prev

    # ---------------------------------------------------------------- 영수증·캡처
    def capture(self, cid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def captures(self, status: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM captures", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql + " ORDER BY created_at, id", args)]

    def add_capture(self, cid: str, *, filename: str, png: bytes, ocr_text: str, parsed: str, origin: str = "screen") -> None:
        if self.captures_dir is None:
            raise RuntimeError("메모리 DB에는 이미지를 저장할 곳이 없음")
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        (self.captures_dir / f"{cid}.png").write_bytes(png)
        with self._lock, self._db:
            self._db.execute("INSERT INTO captures(id, filename, created_at, ocr_text, parsed, status, origin) "
                             "VALUES (?,?,?,?,?,'read',?)", (cid, filename, _now(), ocr_text, parsed, origin))

    def set_capture(self, cid: str, *, status: str, meal_id: Optional[int] = None) -> None:
        with self._lock, self._db:
            if not self._db.execute("UPDATE captures SET status=?, meal_id=? WHERE id=?", (status, meal_id, cid)).rowcount:
                raise NotFound(f"없는 캡처: {cid}")

    def capture_image(self, cid: str) -> Path:
        p = (self.captures_dir or Path("/nonexistent")) / f"{cid}.png"
        if not re.fullmatch(r"[0-9a-f]{24}", cid) or not p.is_file():
            raise NotFound(f"없는 이미지: {cid}")
        return p

    # ---------------------------------------------------------------- 대화 도우미 대화
    def add_conversation(self, cid: str, backend: str, claude_session: Optional[str]) -> None:
        now = _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO conversations(id, backend, claude_session, created_at, updated_at) VALUES (?,?,?,?,?)",
                             (cid, backend, claude_session, now, now))

    def mark_claude_started(self, cid: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE conversations SET claude_started=1 WHERE id=?", (cid,))

    def conversation(self, cid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def conversations(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,))]

    def chat_messages(self, cid: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM chat_messages WHERE conv_id=? ORDER BY id", (cid,))]

    def add_chat_message(self, cid: str, role: str, text: str, *, events: Optional[str] = None, cost: Optional[float] = None,
                         title: Optional[str] = None, turn_done: bool = False) -> None:
        now = _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO chat_messages(conv_id, role, text, events, cost, created_at) VALUES (?,?,?,?,?,?)",
                             (cid, role, text, events, cost, now))
            self._db.execute("UPDATE conversations SET updated_at=?, title=COALESCE(title, ?), turns=turns+? WHERE id=?",
                             (now, title, 1 if turn_done else 0, cid))

    def delete_conversation(self, cid: str) -> None:
        with self._lock, self._db:
            if not self._db.execute("DELETE FROM conversations WHERE id=?", (cid,)).rowcount:
                raise NotFound(f"없는 대화: {cid}")

    # ---------------------------------------------------------------- 경험 기억 [틀림]
    def memory_rejects(self) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        with self._lock:
            for r in self._db.execute("SELECT meal_id, signature FROM memory_rejects"):
                out.setdefault(r["meal_id"], set()).add(r["signature"])
        return out

    def set_memory_reject(self, meal_id: int, signature: str, on: bool) -> None:
        with self._lock, self._db:
            if on:
                self._db.execute("INSERT OR IGNORE INTO memory_rejects VALUES (?,?,?)", (meal_id, signature, _now()))
            else:
                self._db.execute("DELETE FROM memory_rejects WHERE meal_id=? AND signature=?", (meal_id, signature))

    # ---------------------------------------------------------------- A2A 작업 기록
    def add_task(self, tid: str, *, context_id: str, message_id: Optional[str], skill: str, state: str,
                 request: str, pending: Optional[str] = None, result: Optional[str] = None,
                 error: Optional[str] = None) -> None:
        now = _now()
        with self._lock, self._db:
            self._db.execute("INSERT INTO a2a_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (tid, context_id, message_id, skill, state, request, pending, result, error, now, now))

    def task(self, tid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM a2a_tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def task_by_message(self, message_id: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM a2a_tasks WHERE message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None

    def tasks(self, context_id: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM a2a_tasks", []
        if context_id:
            sql += " WHERE context_id=?"
            args.append(context_id)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql + " ORDER BY created_at DESC, rowid DESC", args)]

    def update_task(self, tid: str, **fields) -> None:
        cols = {k: v for k, v in fields.items() if k in ("state", "pending", "result", "error")}
        with self._lock, self._db:
            self._db.execute(f"UPDATE a2a_tasks SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE id=?",
                             [*cols.values(), _now(), tid])

    # ---------------------------------------------------------------- 쓰기
    def _write_items(self, meal_id: int, items: Iterable[dict]) -> None:
        self._db.execute("DELETE FROM meal_items WHERE meal_id=?", (meal_id,))
        self._db.executemany("INSERT INTO meal_items(meal_id, name, qty, kcal_manual, guessed) VALUES (?,?,?,?,0)",
                             [(meal_id, it["name"], it.get("qty", 1), it.get("kcal")) for it in items])

    def add_manual(self, *, day: date, slot: str, items: list[dict], people: int = 1, place: Optional[str] = None,
                   note: Optional[str] = None, time: Optional[str] = None, skipped: bool = False) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO meals(date, slot, time, place, source, people, note, confirmed, slot_locked, menu_user, "
                "skipped, updated_at) VALUES (?,?,?,?,'manual',?,?,1,1,1,?,?)",
                (day.isoformat(), slot, time, place, people, note, int(skipped), _now()))
            self._write_items(cur.lastrowid, [] if skipped else items)
            return cur.lastrowid

    def update_meal(self, meal_id: int, changes: dict) -> None:
        """changes: date·slot(옮기면 동기화가 덮어쓰지 않음), items(메뉴 확정), people, note, place·time(직접 입력만),
        confirmed, excluded."""
        with self._lock, self._db:
            old = self._db.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
            if not old:
                raise NotFound(f"없는 끼니: {meal_id}")
            if old["source"] == "card" and ({"place", "time"} & changes.keys()):
                raise NotAllowed("카드 결제의 가게·시각은 card_spending에서 온 값이라 여기서 바꾸지 않음")
            cols = {k: changes[k] for k in ("date", "slot", "people", "note", "place", "time", "store") if k in changes}
            if "date" in cols or "slot" in cols:
                cols["slot_locked"] = 1
            for flag in ("confirmed", "excluded"):
                if flag in changes:
                    cols[flag] = int(bool(changes[flag]))
            if "items" in changes:
                self._write_items(meal_id, changes["items"])
                cols["menu_user"] = 1
                cols.setdefault("confirmed", 1)
            if cols:
                self._db.execute(f"UPDATE meals SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE id=?",
                                 [*cols.values(), _now(), meal_id])

    def delete_meal(self, meal_id: int) -> None:
        with self._lock, self._db:
            old = self._db.execute("SELECT source FROM meals WHERE id=?", (meal_id,)).fetchone()
            if not old:
                raise NotFound(f"없는 끼니: {meal_id}")
            if old["source"] == "card":
                raise NotAllowed("카드 결제 끼니는 지울 수 없음 (다음 동기화 때 다시 생김) -- '내가 먹은 게 아님'으로 제외")
            self._db.execute("DELETE FROM meals WHERE id=?", (meal_id,))
