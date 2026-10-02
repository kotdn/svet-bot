#!/usr/bin/env python3
"""Предупреждения об отключениях света (YASNO, Київ) в Telegram-группу.
Запускается GitHub Actions каждые 5 минут. Состояние хранится в state.json."""

import json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

GROUP = os.getenv("GROUP", "6.1")
LEAD_MIN = int(os.getenv("LEAD_MIN", "30"))      # за сколько минут предупреждать
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

API = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
NO_SCHEDULE = ("EmergencyShutdowns", "WaitingForSchedule")


def http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 (svet-bot)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"days": {}, "sent": [], "last_run_day": None}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def ddmm(d):
    return f"{d[8:10]}.{d[5:7]}"


def day_info(raw, fallback_date):
    """-> (дата 'YYYY-MM-DD', статус, [[начало, конец], ...] в минутах от полуночи)"""
    if not raw:
        return None
    d = (raw.get("date") or "")[:10] or fallback_date.isoformat()
    slots = sorted((s["start"], s["end"]) for s in raw.get("slots", []) if s.get("type") == "Definite")
    merged = []
    for s, e in slots:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return d, raw.get("status"), merged


def describe(status, outs):
    if status == "EmergencyShutdowns":
        return "⚠️ Екстрені відключення — графіки не діють"
    if status == "WaitingForSchedule":
        return "⏳ Графік ще не опубліковано"
    if not outs:
        return "✅ Відключень немає"
    return "\n".join(f"🔴 {hm(s)}–{hm(e)}" for s, e in outs)


def main():
    now = datetime.now(TZ)
    today = now.date()
    st = load_state()

    try:
        data = http_json(API)
    except Exception as e:
        print("Не удалось получить график:", e)
        return
    g = data.get(GROUP)
    if not g:
        print("Очередь не найдена. Есть:", list(data)[:30])
        return

    infos = [i for i in (day_info(g.get("today"), today),
                         day_info(g.get("tomorrow"), today + timedelta(days=1))) if i]
    msgs, sent = [], set(st["sent"])

    # 1. Новый или изменённый график
    for d, status, outs in infos:
        sig = [status, outs]
        old = st["days"].get(d)
        if old == sig:
            continue
        st["days"][d] = sig
        if old is None and status == "WaitingForSchedule":
            continue
        when = "сьогодні" if d == today.isoformat() else "завтра"
        first = old is None or old[0] == "WaitingForSchedule"
        title = "📅 Графік" if first else "🔄 Зміни в графіку"
        msgs.append(f"{title} на {when} ({ddmm(d)}), черга {GROUP}:\n{describe(status, outs)}")

    # 2. Предупреждения перед отключением и о включении
    periods = []
    for d, status, outs in infos:
        if status in NO_SCHEDULE:
            continue
        base = datetime.fromisoformat(d).replace(tzinfo=TZ)
        periods += [[base + timedelta(minutes=s), base + timedelta(minutes=e)] for s, e in outs]
    periods.sort()
    merged = []
    for s, e in periods:                       # склеиваем отключения через полночь
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    for s, e in merged:
        k_off = f"off-{s:%Y-%m-%dT%H:%M}"
        if s - timedelta(minutes=LEAD_MIN) <= now < e and k_off not in sent:
            if now < s:
                mins = max(1, int((s - now).total_seconds() // 60))
                msgs.append(f"⚠️ Через {mins} хв відключення світла: {s:%H:%M}–{e:%H:%M}")
            else:
                msgs.append(f"🔴 За графіком зараз світла немає, увімкнення о {e:%H:%M}")
            sent.add(k_off)
        k_on = f"on-{e:%Y-%m-%dT%H:%M}"
        if e <= now < e + timedelta(minutes=60) and k_on not in sent:
            msgs.append(f"🟢 За графіком світло має з’явитися ({e:%H:%M})")
            sent.add(k_on)

    for m in msgs:
        send(m)
    print(f"{now:%d.%m %H:%M} черга {GROUP}: "
          + "; ".join(f"{ddmm(d)} {status} відключень {len(outs)}" for d, status, outs in infos)
          + f" | надіслано повідомлень: {len(msgs)}")

    keep_from = (today - timedelta(days=1)).isoformat()
    st["days"] = {d: v for d, v in st["days"].items() if d >= keep_from}
    st["sent"] = sorted(k for k in sent if k.split("-", 1)[1][:10] >= keep_from)
    st["last_run_day"] = today.isoformat()   # раз в день коммит — GitHub не отключит расписание
    save_state(st)


if __name__ == "__main__":
    main()
