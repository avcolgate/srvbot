"""Картинка «Сервер» за сутки, 3 дня, неделю или месяц: светлые карточки с CPU, RAM и трафиком клиентов.
Pillow импортируется только здесь и только при рисовании — в остальное время бот его не держит в памяти."""
import ctypes
import io
from datetime import datetime, timedelta

from . import config
from .util import fmt_bytes

HOUR = 3600
DAY = 24 * HOUR
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# дни -> (название, шаг линий CPU/RAM, шаг столбиков трафика, подпись шага)
PERIODS = {1: ("сутки", HOUR, HOUR, "по часам"), 3: ("3 дня", HOUR, 3 * HOUR, "по 3 часа"),
           7: ("неделя", 3 * HOUR, 6 * HOUR, "по 6 часов"), 30: ("месяц", 12 * HOUR, DAY, "по дням")}

JPEG_Q = 85  # на глаз не отличить от 92, а файл меньше — быстрее загрузка в Telegram
OVER = {1: "за сутки", 3: "за 3 дня", 7: "за неделю", 30: "за месяц"}

S = 2                      # рисуем в двойном размере и уменьшаем — сглаженные линии и текст
W, H = 1080, 1920          # 9:16 — вертикально, во весь экран телефона
PAD, HEAD, GAP, RADIUS = 48, 210, 36, 48
CARD_H = (H - HEAD - PAD - 2 * GAP) // 3
TEXT, MUTED, GRID = (17, 24, 39), (75, 85, 99), (17, 24, 39, 26)
CPU, RAM, NET = (59, 110, 200), (205, 120, 60), (40, 140, 115)
BG, CARD, BORDER = (241, 244, 249), (255, 255, 255), (226, 232, 240)

_BASE: list = []  # фон, тени, карточки и сетка — одинаковы на всех картинках: рисуем один раз за жизнь бота


class ChartError(Exception):
    pass


def _nice(v: float, steps=(1, 2, 2.5, 5, 10)) -> float:
    """Ближайшая «круглая» верхняя граница шкалы ≥ v."""
    if v <= 0:
        return 1
    mag = 10 ** (len(str(int(v))) - 1)
    return next(s * mag for s in steps if s * mag >= v)


def _byte_top(v: float) -> float:
    """Круглая граница шкалы трафика в единицах, которые показывает fmt_bytes (1 ГБ = 2**30)."""
    unit = next((u for u in (2**40, 2**30, 2**20, 2**10) if v >= u), 1)
    return _nice(v / unit) * unit


def _offset(now: float) -> float:
    return datetime.fromtimestamp(now, config.TZ).utcoffset().total_seconds()


def buckets(points: list[tuple[float, float, float]], start: float, end: float, step: int, mean: bool) -> list:
    """Почасовые точки (начало часа, значение, вес) -> [(начало ячейки, значение или None)] на оси [start, end).
    start и end выровнены по местному времени (axis_end), шаг делит шаг столбиков; mean — взвешенное среднее,
    иначе сумма."""
    cells = {start + i * step: [0.0, 0.0] for i in range(int((end - start) // step))}
    for ts, v, w in points:
        c = cells.get(start + (ts - start) // step * step)
        if c is not None:
            c[0] += v * w if mean else v
            c[1] += w
    return [(t, None if not w else (s / w if mean else s)) for t, (s, w) in sorted(cells.items())]


def axis_end(now: float, days: int) -> float:
    """Правый край оси — конец текущей ячейки столбиков: тогда и последний столбик полной ширины."""
    step, off = PERIODS[days][2], _offset(now)
    return (now + off) // step * step - off + step


def ticks(now: float, days: int) -> list[tuple[float, str]]:
    """Подписи оси времени (now — правый край оси): каждые 6 часов за сутки, полночи за 3 дня и неделю,
    каждые 5 дней за месяц."""
    loc = datetime.fromtimestamp(now, config.TZ)
    first = loc - timedelta(days=days)
    if days == 1:
        t = first.replace(minute=0, second=0, microsecond=0) + timedelta(hours=6 - first.hour % 6 or 6)
        step = timedelta(hours=6)
    else:
        t, step = first.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1), timedelta(days=1)
    out = []
    while t < loc:
        if days == 1:
            out.append((t.timestamp(), t.strftime("%H:%M")))
        elif days == 30:
            if t.day % 5 == 0:
                out.append((t.timestamp(), t.strftime("%d.%m")))
        else:
            out.append((t.timestamp(), f"{WEEKDAYS[t.weekday()]} {t.day}" if days == 7 else t.strftime("%d.%m")))
        t += step
    return out


def _x(ts: float, now: float, days: int, x0: float, x1: float) -> float:
    return x0 + (ts - (now - days * DAY)) / (days * DAY) * (x1 - x0)


def _plot_box(y: float) -> tuple[float, float, float, float]:
    """Область графика в карточке, которая начинается на высоте y."""
    return PAD + 150, W - PAD - 48, y + 212, y + CARD_H - 72


def _cards_y() -> list[float]:
    return [HEAD + i * (CARD_H + GAP) for i in range(3)]


def _over_white(rgba) -> tuple:
    """Цвет с прозрачностью поверх белой карточки — как непрозрачный: рисовать его можно без слоя."""
    *rgb, a = rgba
    return tuple(round(255 - (255 - c) * a / 255) for c in rgb)


class _Canvas:
    def __init__(self):
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageFont
        except ImportError as e:
            raise ChartError("для графиков нужен Pillow: apt install python3-pil fonts-dejavu-core") from e
        self.Image, self.ImageDraw, self.ImageFilter = Image, ImageDraw, ImageFilter
        try:
            self.font = lambda size, bold=False: ImageFont.truetype(FONT_BOLD if bold else FONT, size * S)
            self.font(10)
        except OSError as e:
            raise ChartError("нет шрифта DejaVu: apt install fonts-dejavu-core") from e
        self.size = (W * S, H * S)
        if not _BASE:
            _BASE.append(self._base())
        self.img = _BASE[0].copy()  # холст в RGB: всё дальше рисуется прямо на нём, без слоёв
        self.d = ImageDraw.Draw(self.img)

    def _base(self):
        """Фон, мягкие тени (размытые в четверть размера), белые карточки с рамкой и линии сетки."""
        img = self.Image.new("RGBA", self.size, BG + (255,))
        d = self.ImageDraw.Draw(img)
        w, h, r, sm, q = W - 2 * PAD, CARD_H, RADIUS, 30, 4
        sh = self.Image.new("L", ((w + 2 * sm) // q, (h + 2 * sm) // q), 0)
        self.ImageDraw.Draw(sh).rounded_rectangle([sm // q, (sm + 8) // q, (sm + w) // q, (sm + h + 8) // q],
                                                  r // q, fill=34)
        sh = sh.filter(self.ImageFilter.GaussianBlur(4)).resize(((w + 2 * sm) * S, (h + 2 * sm) * S),
                                                                 self.Image.BILINEAR)
        shadow = self.Image.new("RGBA", sh.size, (15, 23, 42, 0))
        shadow.putalpha(sh)
        grid = _over_white(GRID)
        for y in _cards_y():
            img.alpha_composite(shadow, ((PAD - sm) * S, int((y - sm) * S)))
            d.rounded_rectangle([PAD * S, y * S, (W - PAD) * S, (y + h) * S], r * S, fill=CARD, outline=BORDER,
                                width=S)
            x0, x1, y0, y1 = _plot_box(y)
            for frac in (0, 0.5, 1):
                gy = (y1 - frac * (y1 - y0)) * S
                d.line([(x0 * S, gy), (x1 * S, gy)], fill=grid, width=2 * S)
        return img.convert("RGB")

    def text(self, xy, s, size, fill=TEXT, bold=False, anchor="la"):
        self.d.text((xy[0] * S, xy[1] * S), s, font=self.font(size, bold), fill=fill, anchor=anchor)

    def line(self, pts, fill, width):
        self.d.line([(x * S, y * S) for x, y in pts], fill=fill, width=width * S, joint="curve")

    def area(self, pts, base_y, color):
        """Заливка под линией — градиент от полупрозрачного цвета к прозрачному (цвет вливается через маску)."""
        if len(pts) < 2:
            return
        px = [(pts[0][0] * S, base_y * S)] + [(x * S, y * S) for x, y in pts] + [(pts[-1][0] * S, base_y * S)]
        x0, y0, x1, y1 = int(px[0][0]), int(min(y for _, y in px)), int(px[-1][0]) + 1, int(base_y * S) + 1
        size = (x1 - x0, y1 - y0)
        mask = self.Image.new("L", size, 0)
        self.ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in px], fill=255)
        grad = self.Image.linear_gradient("L").rotate(180).resize(size).point(lambda v: v * 110 // 255)
        self.img.paste(color, (x0, y0, x1, y1), self.Image.composite(grad, mask, mask))

    def bar(self, box, color):
        # целые координаты и радиус с запасом: Pillow 10 падает на дробных, когда скругление близко
        # к половине высоты (низкий столбик — мало трафика)
        x0, y0, x1, y1 = (round(v * S) for v in box)
        r = max(0, min(6 * S, (x1 - x0) // 2 - 1, (y1 - y0) // 2 - 1))
        self.d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=color)

    def jpeg(self) -> bytes:
        """JPEG, а не PNG: Telegram всё равно пережимает фото в JPEG, а сжатие PNG на этом сервере — секунда."""
        out = io.BytesIO()
        img = self.img.reduce(S)  # среднее по блокам S×S — быстро и для сглаживания достаточно
        self.img = None  # большой холст больше не нужен — освобождаем до сжатия
        img.save(out, "JPEG", quality=JPEG_Q)
        return out.getvalue()


def _card(c: _Canvas, y: float, title: str, value: str, note: str, color, top: float, fmt, end: float, days: int,
          value_note: str = ""):
    """Подписи карточки (сама карточка и сетка — в основе холста; end — правый край оси);
    возвращает область графика (x0, x1, y0, y1)."""
    c.d.ellipse([(PAD + 44) * S, (y + 47) * S, (PAD + 60) * S, (y + 63) * S], fill=color)
    c.text((PAD + 74, y + 38), title, 34, MUTED)
    c.text((W - PAD - 44, y + 42), note, 28, MUTED, anchor="ra")
    c.text((PAD + 42, y + 84), value, 72, TEXT, bold=True)
    if value_note:  # что за число: «сейчас» — мгновенное, в отличие от графика со средними
        c.text((PAD + 42 + c.font(72, True).getlength(value) / S + 18, y + 128), value_note, 30, MUTED)
    x0, x1, y0, y1 = _plot_box(y)
    for frac in (0, 0.5, 1):
        c.text((x0 - 18, y1 - frac * (y1 - y0)), fmt(top * frac), 26, MUTED, anchor="rm")
    for ts, label in ticks(end, days):
        c.text((_x(ts, end, days, x0, x1), y1 + 18), label, 26, MUTED, anchor="ma")
    return x0, x1, y0, y1


def _series(c, cells, step, now, end, days, box, top, color):
    """Линия по средним за ячейку; пропуски в данных — разрывы линии."""
    x0, x1, y0, y1 = box
    runs, cur = [], []
    for ts, v in cells:
        if v is None:
            runs, cur = runs + [cur] if cur else runs, []
            continue
        cur.append((_x(min(ts + step / 2, now), end, days, x0, x1), y1 - min(v, top) / top * (y1 - y0)))
    if cur:
        runs.append(cur)
    for run in runs:
        c.area(run, y1, color)
        if len(run) > 1:
            c.line(run, color, 5)


def _trim():
    """Вернуть системе освобождённую память. Картинки рисуются в потоках пула, у каждого — своя арена glibc,
    и большие освобождённые блоки оседают в аренах: без этого бот после десятка картинок держит ~250 МБ."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # не glibc — ничего страшного, просто память вернётся позже
        pass


def render(metrics: list | None, series: list | None, now: float, cur_cpu: float, cur_ram: float,
           days: int = 1) -> bytes:
    try:
        return _render(metrics, series, now, cur_cpu, cur_ram, days)
    finally:
        _trim()


def _render(metrics: list | None, series: list | None, now: float, cur_cpu: float, cur_ram: float,
            days: int = 1) -> bytes:
    """JPEG за days дней (1, 3, 7, 30): CPU и RAM — из почасовых средних (trend.add_sample),
    трафик — из почасового ряда всех клиентов (report.account, поле series)."""
    name, line_step, bar_step, bar_note = PERIODS[days]
    end = axis_end(now, days)
    start = end - days * DAY
    cpu = buckets([(x[0], x[1] / x[3], x[3]) for x in metrics or []], start, end, line_step, mean=True)
    ram = buckets([(x[0], x[2] / x[3], x[3]) for x in metrics or []], start, end, line_step, mean=True)
    net = buckets([(x[0], x[1] + x[2], 1) for x in series or []], start, end, bar_step, mean=False)

    c = _Canvas()
    c.text((PAD + 8, 70), f"Сервер · {name}", 64, bold=True)
    loc = datetime.fromtimestamp(now, config.TZ)
    since = datetime.fromtimestamp(now - days * DAY, config.TZ)
    c.text((PAD + 8, 150), f"{since:%d.%m} – {loc:%d.%m.%Y, %H:%M}", 32, MUTED)
    y = HEAD
    pct = lambda v: f"{v:.0f}%"

    peak = max((v for _, v in cpu if v is not None), default=0)
    top = _nice(max(peak * 1.2, 10)) if peak < 80 else 100
    box = _card(c, y, "CPU", pct(cur_cpu), f"пик {peak:.0f}%", CPU, top, pct, end, days, "сейчас")
    _series(c, cpu, line_step, now, end, days, box, top, CPU)
    y += CARD_H + GAP

    avg = [v for _, v in ram if v is not None]
    box = _card(c, y, "RAM", pct(cur_ram), f"в среднем {sum(avg) / len(avg):.0f}%" if avg else "", RAM, 100, pct,
                end, days, "сейчас")
    _series(c, ram, line_step, now, end, days, box, 100, RAM)
    y += CARD_H + GAP

    vals = [v or 0 for _, v in net]
    total = sum(vals)
    top = _byte_top(max(vals) * 1.1) if total else 1
    box = _card(c, y, "Трафик клиентов", fmt_bytes(total), bar_note, NET, top,
                lambda v: fmt_bytes(v) if v else "0", end, days, OVER[days])
    x0, x1, y0, y1 = box
    for ts, v in net:
        if v:
            bx0, bx1 = _x(ts, end, days, x0, x1), _x(ts + bar_step, end, days, x0, x1)
            gap = min(4, (bx1 - bx0) / 5)
            c.bar((max(bx0 + gap, x0), y1 - max(v / top * (y1 - y0), 6), min(bx1 - gap, x1), y1), NET)
    return c.jpeg()
