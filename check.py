#!/usr/bin/env python3
"""Попередження про відключення світла (YASNO, Київ) у Telegram-групу.
Пише тільки коли є відключення: новий графік, зміни, скасування, нагадування.
Вночі (NIGHT_FROM..NIGHT_TO) мовчить; накопичені зміни надсилає вранці одним повідомленням."""

import json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

GROUP = os.getenv("GROUP", "6.1")
LEAD_MIN = int(os.getenv("LEAD_MIN", "30"))       # за скільки хвилин попереджати про відключення
ON_LEAD_MIN = int(os.getenv("ON_LEAD_MIN", "5"))  # за скільки хвилин писати, що світло має з’явитися
NIGHT_FROM = int(os.getenv("NIGHT_FROM", "22"))   # тиша з 22:00
NIGHT_TO = int(os.getenv("NIGHT_TO", "8"))        # до 08:00
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
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("ann", {})      # дата -> графік, про який уже повідомили
    st.setdefault("sent", [])
    return st


def save_state(st):
    st.pop("days", None)          # поле зі старої версії
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def ddmm(d):
    return f"{d[8:10]}.{d[5:7]}"


def day_info(raw, fallback_date):
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


def is_night(now):
    h = now.hour
    return h >= NIGHT_FROM or h < NIGHT_TO


def has_outages(sig):
    return bool(sig) and (sig[0] == "EmergencyShutdowns" or (sig[0] not in NO_SCHEDULE and sig[1]))


def main():
    now = datetime.now(TZ)
    today = now.date()
    night = is_night(now)
    st = load_state()

    try:
        data = http_json(API)
    except Exception as e:
        print("Не вдалося отримати графік:", e)
        return
    g = data.get(GROUP)
    if not g:
        print("Чергу не знайдено. Є:", list(data)[:30])
        return

    infos = [i for i in (day_info(g.get("today"), today),
                         day_info(g.get("tomorrow"), today + timedelta(days=1))) if i]
    msgs, sent = [], set(st["sent"])

    # 1. Графіки: тільки якщо є відключення (або їх скасували). Вночі — відкладаємо до ранку.
    if not night:
        for d, status, outs in infos:
            if status == "WaitingForSchedule":
                continue
            sig, old = [status, outs], st["ann"].get(d)
            if sig == old:
                continue
            when = "сьогодні" if d == today.isoformat() else "завтра"
            head = f"на {when} ({ddmm(d)}), черга {GROUP}"
            if status == "EmergencyShutdowns":
                msgs.append(f"⚠️ Екстрені відключення {head} — графіки не діють")
            elif outs:
                title = "🔄 Зміни в графіку" if has_outages(old) else "📅 Графік відключень"
                msgs.append(f"{title} {head}:\n" + "\n".join(f"🔴 {hm(s)}–{hm(e)}" for s, e in outs))
            elif has_outages(old):
                msgs.append(f"✅ Відключення {head} скасовано")
            st["ann"][d] = sig

    # 2. Нагадування перед відключенням і про ввімкнення (не вночі)
    periods = []
    for d, status, outs in infos:
        if status in NO_SCHEDULE:
            continue
        base = datetime.fromisoformat(d).replace(tzinfo=TZ)
        periods += [[base + timedelta(minutes=s), base + timedelta(minutes=e)] for s, e in outs]
    periods.sort()
    merged = []
    for s, e in periods:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    for s, e in merged:
        k_off, k_on = f"off-{s:%Y-%m-%dT%H:%M}", f"on-{e:%Y-%m-%dT%H:%M}"
        if s - timedelta(minutes=LEAD_MIN) <= now < s and k_off not in sent:
            if not night:
                mins = max(1, int((s - now).total_seconds() // 60))
                msgs.append(f"⚠️ Через {mins} хв відключення світла: {s:%H:%M}–{e:%H:%M}")
            sent.add(k_off)
        if e - timedelta(minutes=ON_LEAD_MIN) <= now < e + timedelta(minutes=60) and k_on not in sent:
            if not night:
                if now < e:
                    mins = max(1, -(-int((e - now).total_seconds()) // 60))
                    msgs.append(f"🟢 Через {mins} хв за графіком світло має з’явитися ({e:%H:%M})")
                else:
                    msgs.append(f"🟢 За графіком світло має з’явитися ({e:%H:%M})")
            sent.add(k_on)

    for m in msgs:
        send(m)
    print(f"{now:%d.%m %H:%M} черга {GROUP}{' (ніч)' if night else ''}: "
          + "; ".join(f"{ddmm(d)} {status} відключень {len(outs)}" for d, status, outs in infos)
          + f" | надіслано повідомлень: {len(msgs)}")

    keep_from = (today - timedelta(days=1)).isoformat()
    st["ann"] = {d: v for d, v in st["ann"].items() if d >= keep_from}
    st["sent"] = sorted(k for k in sent if k.split("-", 1)[1][:10] >= keep_from)
    st["last_run_day"] = today.isoformat()
    save_state(st)


if __name__ == "__main__":
    main()
