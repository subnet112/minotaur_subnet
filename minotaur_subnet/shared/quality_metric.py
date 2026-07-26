"""The App-declared QUALITY METRIC contract.

Minotaur's champion contest compares a challenger against the champion
**per order** on one number per order. Historically that number was the JS
scorer's ``score`` (a 0..1 float, mirrored on-chain as ``scoreIntent`` BPS).
For a DEX aggregator that is the wrong quantity — the right one is the amount
actually delivered — so ``metadata.raw_output`` was wired in as the per-order
signal, and the adoption rule was written around it: exact-integer wei,
higher-is-better, and a validity gate that REJECTS a submission which
delivered nothing on every order.

That is correct for a DEX and wrong as a platform rule. An app whose orders
legitimately move no tokens to a receiver — a rebalancing vault, whose quality
is something like tracking error, and possibly *lower is better* — cannot
produce a positive delivered amount, so every one of its submissions is
rejected as invalid and it can never hold a champion. The DEX special case
didn't extend the generic metric, it displaced it.

This module gives the number back its generality: an app DECLARES what its
quality metric is, and the platform compares whatever that is. Nothing in core
needs to know what a swap is.

    "quality_metric": {
      "source":   "js_metadata" | "js_score",
      "field":    "raw_output",            // js_metadata only
      "polarity": "higher_better" | "lower_better",
      "scale":    "integer" | "bps" | "ratio_1",
      "validity": "positive" | "any_row"
    }

The default is exactly today's DEX behaviour, so an app that declares nothing
is scored bit-identically to before.

**The metric always comes from the app's JS.** There is no "read the on-chain
score directly" source: the contract's ``scoreIntent`` BPS is exposed TO the
scorer (``context.simulation.on_chain_score``), so an app that wants the
contract's verdict returns it — or a function of it — from its own JS. One
source of truth, and the app stays in charge of what quality means for it.

DETERMINISM. This feeds the sole adoption gate, so every conversion here is
exact-integer and host-independent. ``lower_better`` requires a BOUNDED scale
(``bps``/``ratio_1``) because inverting an unbounded integer has no exact
form; the combination is rejected at resolve time rather than silently
mis-ranking. ``ratio_1`` quantises to basis points, which is the same
resolution the comparison band already works in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Bounded scales, and the integer ceiling each normalises to.
_SCALE_MAX: dict[str, int] = {"bps": 10_000, "ratio_1": 10_000}

_SOURCES = ("js_metadata", "js_score")
_POLARITIES = ("higher_better", "lower_better")
_SCALES = ("integer", "bps", "ratio_1")
_VALIDITIES = ("positive", "any_row")


class QualityMetricError(ValueError):
    """An app declared a quality metric the platform can't compare."""


@dataclass(frozen=True)
class QualityMetric:
    """How to read, normalise and validate one app's per-order quality."""

    source: str = "js_metadata"
    field: str = "raw_output"
    polarity: str = "higher_better"
    scale: str = "integer"
    validity: str = "positive"

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source, "field": self.field,
            "polarity": self.polarity, "scale": self.scale,
            "validity": self.validity,
        }


#: Today's behaviour, unchanged: the DEX aggregator's delivered wei amount,
#: higher-is-better, and a submission is only valid if it delivered on >= 1
#: order. Every app that declares nothing gets this.
DEFAULT = QualityMetric()


def resolve(manifest: Any) -> QualityMetric:
    """Read an app's declared metric out of its manifest.

    Absent/malformed declaration → DEFAULT, so this can be rolled out ahead of
    any app declaring anything. An INVALID declaration raises: a metric the
    platform can't compare must fail loudly at resolve time, never silently
    mis-rank a champion contest.
    """
    if not isinstance(manifest, dict):
        return DEFAULT
    raw = manifest.get("quality_metric")
    if not isinstance(raw, dict) or not raw:
        return DEFAULT

    metric = QualityMetric(
        source=str(raw.get("source", DEFAULT.source)),
        field=str(raw.get("field", DEFAULT.field)),
        polarity=str(raw.get("polarity", DEFAULT.polarity)),
        scale=str(raw.get("scale", DEFAULT.scale)),
        validity=str(raw.get("validity", DEFAULT.validity)),
    )
    _validate(metric)
    return metric


def _validate(m: QualityMetric) -> None:
    if m.source not in _SOURCES:
        raise QualityMetricError(f"quality_metric.source must be one of {_SOURCES}")
    if m.polarity not in _POLARITIES:
        raise QualityMetricError(f"quality_metric.polarity must be one of {_POLARITIES}")
    if m.scale not in _SCALES:
        raise QualityMetricError(f"quality_metric.scale must be one of {_SCALES}")
    if m.validity not in _VALIDITIES:
        raise QualityMetricError(f"quality_metric.validity must be one of {_VALIDITIES}")
    if m.source == "js_metadata" and not m.field:
        raise QualityMetricError("quality_metric.field is required for js_metadata")
    if m.polarity == "lower_better" and m.scale not in _SCALE_MAX:
        raise QualityMetricError(
            "quality_metric.polarity=lower_better needs a bounded scale "
            f"({'/'.join(_SCALE_MAX)}) — an unbounded integer has no exact inverse"
        )


# ── Row plumbing ────────────────────────────────────────────────────────────
#
# The resolved contract is persisted ON the benchmark row, not looked up at
# comparison time: the adoption decision must be reproducible from the stored
# artifact alone (same compute-once-read-forever discipline as
# max_region_nodes / content_fingerprint), and it keeps epoch/relative_scoring
# a pure function of the rows it is handed. A DEFAULT contract is stored as
# nothing at all, so today's rows gain zero bytes and legacy rows read
# correctly.


def to_row(metric: QualityMetric) -> dict[str, str] | None:
    return None if metric == DEFAULT else metric.to_dict()


def from_row(row: Any) -> QualityMetric:
    """Read the contract off a persisted row; DEFAULT when absent."""
    if row is None:
        return DEFAULT
    raw = row.get("metric") if isinstance(row, dict) else getattr(row, "metric", None)
    if not isinstance(raw, dict) or not raw:
        return DEFAULT
    try:
        metric = QualityMetric(
            source=str(raw.get("source", DEFAULT.source)),
            field=str(raw.get("field", DEFAULT.field)),
            polarity=str(raw.get("polarity", DEFAULT.polarity)),
            scale=str(raw.get("scale", DEFAULT.scale)),
            validity=str(raw.get("validity", DEFAULT.validity)),
        )
        _validate(metric)
    except QualityMetricError:
        # A row carrying an uncomparable contract scores as no-signal rather
        # than crashing a round; the resolve-time raise is the real gate.
        return DEFAULT
    return metric


def extract(result: Any, metric: QualityMetric) -> str | None:
    """Pull the per-order value off a ScoreResult, as an EXACT decimal string.

    Kept as a string end-to-end: the delivered-amount case is wei that can
    exceed 2^53, and float() there would silently lose precision.
    """
    if result is None:
        return None
    if metric.source == "js_score":
        score = getattr(result, "score", None)
        if score is None:
            return None
        # 0..1 float → basis points. Quantising here (rather than comparing
        # floats later) keeps the verdict exact-integer.
        try:
            return str(int(round(float(score) * _SCALE_MAX["ratio_1"])))
        except (TypeError, ValueError):
            return None
    raw = (getattr(result, "metadata", None) or {}).get(metric.field)
    if raw is None or str(raw) == "":
        return None
    return str(raw)


# ── Comparison ──────────────────────────────────────────────────────────────


def comparable(value: Any, metric: QualityMetric) -> int | None:
    """Normalise a stored value to a HIGHER-IS-BETTER exact integer.

    Returns None when there is no usable value — which the caller reads as
    "this order produced no signal", distinct from "produced a zero".
    """
    if value is None or value == "":
        return None
    try:
        if metric.scale == "ratio_1":
            v = int(round(float(value) * _SCALE_MAX["ratio_1"]))
        else:
            v = int(str(value))
    except (TypeError, ValueError):
        return None

    ceiling = _SCALE_MAX.get(metric.scale)
    if ceiling is not None:
        v = max(0, min(ceiling, v))
        if metric.polarity == "lower_better":
            v = ceiling - v
    return v


def has_value(value: Any, metric: QualityMetric) -> bool:
    """Did this order produce a usable signal, per the app's validity rule?

    ``positive`` (default) — the DEX rule: it must have delivered something.
    ``any_row``            — producing the metric at all counts, which is what
                             an app whose orders legitimately deliver no tokens
                             needs in order to be rankable at all.
    """
    v = comparable(value, metric)
    if v is None:
        return False
    return v > 0 if metric.validity == "positive" else True
