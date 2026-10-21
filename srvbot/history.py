"""История действий: что сделано через бота и чем закончилось. Последние записи хранятся в состоянии
(раздел 🗒 История в ⚙️ Управлении) и дублируются в журнал службы."""
import logging
import time
from datetime import datetime

from . import config
from .util import State, esc, fmt_duration

log = logging.getLogger(__name__)

KEEP = 50   # записей в состоянии
SHOW = 20   # записей в разделе


def record(state: State, what: str, ok: bool = True, detail: str = "", took: float | None = None):
    entry = {"ts": time.time(), "what": what, "ok": ok}
    if detail:
        entry["detail"] = detail
    if took is not None:
        entry["took"] = round(took, 1)
    hist = state.get("history", [])
    hist.append(entry)
    del hist[:-KEEP]
    state.save()
    (log.info if ok else log.warning)("действие: %s — %s%s%s", what, "успешно" if ok else "ошибка",
                                      f": {detail}" if detail else "", f" ({took:.1f} с)" if took is not None else "")


def line(e: dict) -> str:
    """«21.10 10:08 ✅ установка обновлений · 6 пакетов · 2 мин»"""
    when = datetime.fromtimestamp(e["ts"], config.TZ).strftime("%d.%m %H:%M")
    parts = [esc(e["what"])] + ([esc(e["detail"])] if e.get("detail") else [])
    if e.get("took") is not None:
        parts.append(fmt_duration(e["took"]) if e["took"] >= 60 else f"{e['took']:.0f} с")
    return f"<code>{when}</code> {'✅' if e['ok'] else '🔴'} " + " · ".join(parts)


def view(state: State) -> str:
    hist = state.data.get("history") or []
    lines = ["<b>🗒 История действий</b>"]
    if not hist:
        lines.append("пока ничего не выполнялось")
    lines += [line(e) for e in reversed(hist[-SHOW:])]
    if len(hist) > SHOW:
        lines.append(f"<i>показаны последние {SHOW} из {len(hist)}</i>")
    return "\n".join(lines)
