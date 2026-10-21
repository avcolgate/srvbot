"""Наглядность: полоски заполнения из цветных эмодзи и почасовая история CPU и RAM в состоянии бота
(переживает перезапуски; по ней рисуется картинка «Сервер за сутки» — см. chart.py)."""
HOUR = 3600
DAY = 24 * HOUR
KEEP = 31 * DAY  # история для картинки «за месяц»


def bar(pct: float, width: int = 8, warn: float = 75, crit: float = 90) -> str:
    """«🟩🟩⬜⬜⬜⬜⬜⬜» — заполнение в процентах; цвет по порогам: зелёный, жёлтый, красный."""
    cell = "🟥" if pct >= crit else "🟨" if pct >= warn else "🟩"
    n = max(0, min(width, round(pct / 100 * width)))
    return cell * n + "⬜" * (width - n)


def add_sample(hist: list | None, now: float, cpu: float, ram: float) -> list:
    """hist = [[начало часа, сумма CPU %, сумма RAM %, замеров], …] за последний месяц (~20 КБ в состоянии)."""
    hist = hist or []
    hour = int(now // HOUR * HOUR)
    if hist and hist[-1][0] == hour:
        hist[-1][1] += cpu
        hist[-1][2] += ram
        hist[-1][3] += 1
    else:
        hist.append([hour, cpu, ram, 1])
    return [x for x in hist if x[0] > hour - KEEP]
