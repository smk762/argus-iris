"""The operator's policy file (DESIGN.md, "Policy: schedules, keepalive, idle-kill").

```yaml
timezone: America/Los_Angeles        # cron fields are read in this zone; everything else is UTC
windows:                             # a tenant's leases run at `class` while its window is open
  - name: nightly-audit
    cron: "0 1 * * *"
    duration: 5h
    tenant: ai-code-auditor
    target: "ollama:*"
    class: batch
residents:                           # first matching rule wins; ids/models/services are fnmatch patterns
  - match: { node: dragon, ollama: "hf.co/inclusionAI/SingGuard-4b-GGUF:*" }
    keepalive: always                # pinned: never evicted, Ollama keep_alive held at -1
  - match: { node: jarrah, ollama: "*" }
    idle_kill: 5m
    enforce: true                    # act on it; without this iris only reports what it would do
  - match: { resident: "dragon/modernbert" }
    idle_kill: 15m
    restart_residual_mb: 64          # unloaded but holding a CUDA context this big: restart the container
  - match: { resident: "dragon/xtts" }
    idle_kill: 120s                  # GPU -> CPU (evict_path), kept warm in host RAM ...
    unload_after: 600s               # ... then unloaded fully after 10 more minutes idle
```

Each rule acts only with ``enforce: true``, so residents are handed over one at a time (#6): turn a resident's own
timer to -1 and set ``enforce`` in the same change. Without it, the dashboard shows what iris would do.

Cron is the five standard fields (``*``, numbers, ranges, lists, ``*/n`` and ``a-b/n``; day-of-week 0-7 with 0 and 7
Sunday; when both day fields are restricted either may match, as in cron). No dependency beyond the standard library
(``tzdata`` supplies the zones where the OS has none).
"""

from __future__ import annotations

import fnmatch
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from argus_cortex.gpu_wire import PriorityClass
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from argus_iris.models import Resident

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)?\s*$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(value: object) -> float:
    """``90``, ``"90s"``, ``"15m"``, ``"5h"``, ``"1.5d"`` -> seconds."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    m = _DURATION.match(str(value))
    if not m:
        raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 15m, 5h, 2d)")
    return float(m.group(1)) * _UNIT_S[m.group(2)]


Duration = Annotated[float, BeforeValidator(parse_duration), Field(ge=0)]


class Cron:
    """A five-field cron expression, matched minute by minute."""

    _RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron needs five fields (minute hour day month weekday): {expr!r}")
        self.expr = expr
        fields = [self._field(p, lo, hi) for p, (lo, hi) in zip(parts, self._RANGES, strict=True)]
        self.minute, self.hour, self.dom, self.month, dow = fields
        self.dow = {d % 7 for d in dow}
        self._dom_any, self._dow_any = parts[2] == "*", parts[4] == "*"

    @staticmethod
    def _field(spec: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for part in spec.split(","):
            base, _, step_s = part.partition("/")
            step = int(step_s) if step_s else 1
            if base == "*":
                a, b = lo, hi
            elif "-" in base:
                a, b = (int(x) for x in base.split("-", 1))
            else:
                a = int(base)
                b = hi if step_s else a
            if not (lo <= a <= b <= hi) or step < 1:
                raise ValueError(f"cron field {spec!r} outside {lo}-{hi}")
            out.update(range(a, b + 1, step))
        return out

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minute or t.hour not in self.hour or t.month not in self.month:
            return False
        dom, dow = t.day in self.dom, (t.weekday() + 1) % 7 in self.dow
        if self._dom_any or self._dow_any:
            return dom and dow
        return dom or dow

    def open_since(self, now: datetime, duration_s: float) -> datetime | None:
        """The start of a window of *duration_s* open at *now* (in *now*'s zone), or None."""
        t = now.replace(second=0, microsecond=0)
        earliest = now - timedelta(seconds=duration_s)
        while t > earliest:
            if self.matches(t):
                return t
            t -= timedelta(minutes=1)
        return None


def _cron(value: object) -> Cron:
    return value if isinstance(value, Cron) else Cron(str(value))


CronExpr = Annotated[Cron, BeforeValidator(_cron)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class Window(_Model):
    name: str
    cron: CronExpr
    duration: Duration
    tenant: str
    target: str | None = None  # fnmatch on the lease's target
    node: str | None = None
    class_: PriorityClass = Field(alias="class")


class KeepWindow(_Model):
    cron: CronExpr
    duration: Duration


class ResidentMatch(_Model):
    """All set fields must match (fnmatch patterns)."""

    node: str | None = None
    service: str | None = None
    ollama: str | None = None  # the model name of an Ollama resident
    resident: str | None = None  # the resident id, "<node>/<service>[/<model>]"

    def matches(self, r: Resident) -> bool:
        checks = [(self.node, r.node), (self.service, r.service), (self.resident, r.id)]
        if self.ollama is not None:
            if r.source.value != "ollama" or not r.model:
                return False
            checks.append((self.ollama, r.model))
        return all(pat is None or fnmatch.fnmatchcase(val, pat) for pat, val in checks)


class ResidentRule(_Model):
    match: ResidentMatch = ResidentMatch()
    keepalive: Literal["always"] | KeepWindow | None = None
    idle_kill: Duration | None = None
    # For a service with a CPU stage (evict_path): once idle-kill has moved it to the CPU, unload it fully after it
    # has been idle this much longer, freeing host RAM too. 0 unloads at once. The sidecar's own *_KEEP_ALIVE_CPU
    # timer does not run once its *_KEEP_ALIVE_GPU is -1, so iris has to own this stage too.
    unload_after: Duration | None = None
    restart_residual_mb: int | None = Field(None, ge=0)
    enforce: bool = False


class Policy(_Model):
    timezone: str = "UTC"
    windows: list[Window] = Field(default_factory=list)
    residents: list[ResidentRule] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def rule_for(self, r: Resident) -> tuple[int, ResidentRule] | None:
        return next(((i, rule) for i, rule in enumerate(self.residents) if rule.match.matches(r)), None)

    def kept(self, rule: ResidentRule, now: datetime) -> bool:
        """Whether *rule* pins its resident at *now*."""
        if rule.keepalive == "always":
            return True
        if isinstance(rule.keepalive, KeepWindow):
            return rule.keepalive.cron.open_since(now.astimezone(self.zone), rule.keepalive.duration) is not None
        return False

    def window_class(self, tenant: str, target: str, node: str | None, now: datetime) -> tuple[str, str] | None:
        """(window name, class) of an open window for this lease, or None."""
        local = now.astimezone(self.zone)
        for w in self.windows:
            if w.tenant != tenant or (w.target and not fnmatch.fnmatchcase(target, w.target)):
                continue
            if w.node and node and w.node != node:
                continue
            if w.cron.open_since(local, w.duration) is not None:
                return w.name, w.class_
        return None


def load_policy(path: str | os.PathLike[str] | None = None) -> Policy:
    """The policy at *path* (or ``IRIS_POLICY``); an empty policy when neither is set."""
    path = path or os.environ.get("IRIS_POLICY")
    if not path:
        return Policy()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Policy.model_validate(data)
