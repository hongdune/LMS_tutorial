"""Visualization widgets for the LMS (runtime contract 4.5). Import as ``lms_viz``.

Each function displays its widget right away (``display_data`` with
``application/vnd.lms.<widget>+json`` plus a ``text/plain`` fallback) and returns a
handle for updating it in place (``update_display_data`` with the full data).

    import lms_viz

    lms_viz.heatmap(attn[0], x_labels=tokens, y_labels=tokens, title="attention")
    lms_viz.scatter(emb[:, 0], emb[:, 1], label=words, group=classes)

    curve = lms_viz.line(title="loss", x_title="step", y_title="loss")
    for step in range(steps):
        ...
        curve.append(step, train=loss.item())        # one or more series per call
        if step % 100 == 0:
            curve.append(step, val=val_loss)

Live updates are throttled (``min_interval``, default 0.2s); whatever is pending is sent
when the cell finishes, so the final state is always shown.

Data may be lists, numpy arrays or torch tensors (moved to CPU). NaN/Infinity become
null. Over the contract limits, data is cropped (heatmap) or evenly downsampled
(line, scatter) and ``"truncated": true`` is set.
"""

from __future__ import annotations

import math
import time
import uuid
import weakref
from typing import Any, Optional

SCHEMA_VERSION = 1
HEATMAP_MAX = 256
LINE_MAX_POINTS = 10_000
LINE_MAX_SERIES = 20
SCATTER_MAX_POINTS = 5_000
MIME = "application/vnd.lms.{}+json"

__all__ = ["heatmap", "line", "scatter", "Heatmap", "Line", "Scatter"]


# --- value conversion --------------------------------------------------------------------

def _num(v):
    """JSON number or None (NaN/Infinity/non-numbers)."""
    if v is None:
        return None
    if not isinstance(v, (int, float)) and hasattr(v, "item"):
        v = v.item()  # numpy scalar or 0-d tensor
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _to_list(data) -> list:
    if hasattr(data, "detach"):  # torch.Tensor
        data = data.detach().cpu()
    if hasattr(data, "tolist"):  # tensor / ndarray
        data = data.tolist()
    return data if isinstance(data, list) else list(data)


def _vector(data) -> list:
    if hasattr(data, "detach") or hasattr(data, "ravel"):
        data = data.reshape(-1) if hasattr(data, "detach") else data.ravel()
    return [_num(v) for v in _to_list(data)]


def _evenly(n: int, limit: int) -> list[int]:
    """Indices of at most `limit` points spread over range(n), first and last included."""
    if n <= limit:
        return list(range(n))
    step = (n - 1) / (limit - 1)
    return sorted({round(i * step) for i in range(limit)})


def _strings(values, n: Optional[int], what: str) -> Optional[list]:
    if values is None:
        return None
    out = [str(v) for v in _to_list(values)]
    if n is not None and len(out) != n:
        raise ValueError(f"{what}의 길이({len(out)})가 데이터 길이({n})와 다릅니다")
    return out


# --- display plumbing ----------------------------------------------------------------------

_pending: "weakref.WeakSet[_Widget]" = weakref.WeakSet()
_hook_registered = False


def _shell():
    try:
        from IPython import get_ipython

        return get_ipython()
    except Exception:
        return None


def _flush_pending(*_):
    for w in list(_pending):
        w.refresh(force=True)


def _register_hook():
    global _hook_registered
    shell = _shell()
    if shell is not None and not _hook_registered:
        shell.events.register("post_run_cell", _flush_pending)
        _hook_registered = True


class _Widget:
    widget = ""

    def __init__(self, title=None, x_title=None, y_title=None):
        self.title, self.x_title, self.y_title = title, x_title, y_title
        self._display_id = uuid.uuid4().hex
        self._handle = None
        self._shown_in: Optional[int] = None

    # subclasses: return (fields, truncated) and the text/plain fallback
    def _fields(self) -> tuple[dict, bool]:
        raise NotImplementedError

    def _text(self) -> str:
        raise NotImplementedError

    def _payload(self) -> dict:
        fields, truncated = self._fields()
        data: dict[str, Any] = {"version": SCHEMA_VERSION, **fields}
        for key in ("title", "x_title", "y_title"):
            if getattr(self, key) is not None:
                data[key] = str(getattr(self, key))
        if truncated:
            data["truncated"] = True
        return data

    def _bundle(self) -> dict:
        text = self._text()
        if self.title:
            text += f" — {self.title}"
        return {MIME.format(self.widget): self._payload(), "text/plain": text}

    def show(self):
        """Display (again) as a new output; later updates go to every copy."""
        from IPython.display import display

        bundle = self._bundle()
        if _shell() is None:
            print(bundle["text/plain"])
            return self
        handle = display(bundle, raw=True, display_id=self._display_id)
        self._handle = self._handle or handle
        shell = _shell()
        self._shown_in = getattr(shell, "execution_count", None)
        return self

    def refresh(self, force: bool = False):
        """Send the full current data to the displayed output(s)."""
        _pending.discard(self)
        if self._handle is not None:
            self._handle.update(self._bundle(), raw=True)
        return self

    def _ipython_display_(self):
        # `lms_viz.heatmap(x)` as the last line of a cell: already displayed, do nothing.
        # Evaluating the handle in a later cell shows it again.
        shell = _shell()
        if self._handle is None or getattr(shell, "execution_count", None) != self._shown_in:
            self.show()

    def __repr__(self):
        return self._text()


# --- heatmap -------------------------------------------------------------------------------

class Heatmap(_Widget):
    widget = "heatmap"

    def __init__(self, data, x_labels=None, y_labels=None, color="sequential", **titles):
        super().__init__(**titles)
        self.update(data, x_labels=x_labels, y_labels=y_labels, color=color, _refresh=False)

    def update(self, data=None, *, x_labels=None, y_labels=None, color=None, _refresh=True):
        if data is not None:
            truncated = False
            shape = getattr(data, "shape", None)
            if shape is not None:
                if len(shape) != 2:
                    raise ValueError(f"heatmap 데이터는 2차원이어야 합니다 (shape={tuple(shape)})")
                truncated = shape[0] > HEATMAP_MAX or shape[1] > HEATMAP_MAX
                data = data[:HEATMAP_MAX, :HEATMAP_MAX]  # crop before copying to CPU / lists
            rows = _to_list(data)
            if any(not hasattr(r, "__len__") or isinstance(r, str) for r in rows):
                raise ValueError("heatmap 데이터는 2차원(행의 목록)이어야 합니다")
            truncated = truncated or len(rows) > HEATMAP_MAX or any(len(r) > HEATMAP_MAX for r in rows)
            self.data = [[_num(v) for v in list(r)[:HEATMAP_MAX]] for r in rows[:HEATMAP_MAX]]
            self._truncated = truncated
        if x_labels is not None:
            self.x_labels = _strings(x_labels, None, "x_labels")
        elif not hasattr(self, "x_labels"):
            self.x_labels = None
        if y_labels is not None:
            self.y_labels = _strings(y_labels, None, "y_labels")
        elif not hasattr(self, "y_labels"):
            self.y_labels = None
        if color is not None:
            if color not in ("sequential", "diverging"):
                raise ValueError("color는 'sequential' 또는 'diverging'입니다")
            self.color = color
        if _refresh:
            self.refresh()
        return self

    def _fields(self):
        fields: dict[str, Any] = {"data": self.data, "color": self.color}
        if self.x_labels is not None:
            fields["x_labels"] = self.x_labels[:HEATMAP_MAX]
        if self.y_labels is not None:
            fields["y_labels"] = self.y_labels[:HEATMAP_MAX]
        return fields, self._truncated

    def _text(self):
        cols = max((len(r) for r in self.data), default=0)
        return f"[lms_viz 히트맵 {len(self.data)}×{cols}]"


def heatmap(data, x_labels=None, y_labels=None, color="sequential", *, title=None,
            x_title=None, y_title=None) -> Heatmap:
    """2-D heatmap (row-major). ``color``: "sequential" or "diverging". Cropped to 256×256."""
    return Heatmap(data, x_labels, y_labels, color, title=title, x_title=x_title,
                   y_title=y_title).show()


# --- line ----------------------------------------------------------------------------------

class Line(_Widget):
    widget = "line"

    def __init__(self, series=None, *, min_interval=0.2, **titles):
        super().__init__(**titles)
        self.series: dict[str, tuple[list, list]] = {}
        self.min_interval = min_interval
        self._last_sent = 0.0
        for name, value in (series or {}).items():
            self.set(name, *(value if isinstance(value, tuple) else (None, value)), _refresh=False)

    def set(self, name: str, x=None, y=None, *, _refresh=True):
        """Replace one series. ``x`` defaults to 0, 1, 2, ..."""
        ys = _vector(y if y is not None else [])
        xs = _vector(x) if x is not None else list(range(len(ys)))
        if len(xs) != len(ys):
            raise ValueError(f"'{name}' 시리즈의 x({len(xs)})와 y({len(ys)}) 길이가 다릅니다")
        self.series[str(name)] = (xs, ys)
        if _refresh:
            self.refresh()
        return self

    def append(self, x, **values):
        """Add one point at ``x`` to each named series, e.g. ``append(step, train=0.9, val=1.1)``."""
        if not values:
            raise TypeError("append(x, 이름=값, ...) 형태로 값을 하나 이상 넘기세요")
        xv = _num(x)
        for name, value in values.items():
            xs, ys = self.series.setdefault(name, ([], []))
            xs.append(xv)
            ys.append(_num(value))
        self._schedule()
        return self

    def _schedule(self):
        if self._handle is None:
            return
        now = time.monotonic()
        if now - self._last_sent >= self.min_interval:
            self.refresh()
        else:
            _pending.add(self)  # sent by the next append past the interval or at cell end
            _register_hook()

    def refresh(self, force: bool = False):
        self._last_sent = time.monotonic()
        return super().refresh(force)

    def _fields(self):
        names = list(self.series)
        truncated = len(names) > LINE_MAX_SERIES
        out = []
        for name in names[:LINE_MAX_SERIES]:
            xs, ys = self.series[name]
            idx = _evenly(len(xs), LINE_MAX_POINTS)
            truncated = truncated or len(idx) < len(xs)
            out.append({"name": name, "x": [xs[i] for i in idx], "y": [ys[i] for i in idx]})
        return {"series": out}, truncated

    def _text(self):
        parts = [f"{n} {len(xy[0])}점" for n, xy in self.series.items()]
        return f"[lms_viz 선 그래프: {', '.join(parts) or '데이터 없음'}]"


def line(series=None, *, x=None, y=None, name="y", title=None, x_title=None, y_title=None,
         min_interval=0.2) -> Line:
    """Line chart. ``series={"train": (xs, ys), "val": ys}`` or ``x=..., y=...``; empty is fine
    for a live curve filled with ``.append(step, train=loss)``."""
    w = Line(series, min_interval=min_interval, title=title, x_title=x_title, y_title=y_title)
    if y is not None:
        w.set(name, x, y, _refresh=False)
    _register_hook()
    return w.show()


# --- scatter -------------------------------------------------------------------------------

class Scatter(_Widget):
    widget = "scatter"

    def __init__(self, x, y, label=None, group=None, **titles):
        super().__init__(**titles)
        self.update(x, y, label=label, group=group, _refresh=False)

    def update(self, x, y, *, label=None, group=None, _refresh=True):
        xs, ys = _vector(x), _vector(y)
        if len(xs) != len(ys):
            raise ValueError(f"x({len(xs)})와 y({len(ys)}) 길이가 다릅니다")
        self.x, self.y = xs, ys
        self.label = _strings(label, len(xs), "label")
        self.group = _strings(group, len(xs), "group")
        if _refresh:
            self.refresh()
        return self

    def _fields(self):
        idx = _evenly(len(self.x), SCATTER_MAX_POINTS)
        fields: dict[str, Any] = {"x": [self.x[i] for i in idx], "y": [self.y[i] for i in idx]}
        if self.label is not None:
            fields["label"] = [self.label[i] for i in idx]
        if self.group is not None:
            fields["group"] = [self.group[i] for i in idx]
        return fields, len(idx) < len(self.x)

    def _text(self):
        return f"[lms_viz 산점도 {len(self.x)}점]"


def scatter(x, y, label=None, group=None, *, title=None, x_title=None, y_title=None) -> Scatter:
    """Scatter plot. ``label``/``group``: one string per point (group = color). Max 5,000 points."""
    return Scatter(x, y, label, group, title=title, x_title=x_title, y_title=y_title).show()
