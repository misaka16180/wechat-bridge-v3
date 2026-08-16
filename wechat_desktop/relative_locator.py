"""Visible screenshot anchor-relative locator for v3.

The locator deliberately does not inspect WeChat's process, accessibility tree,
memory, or private protocol.  It finds small, stable visual anchors in a
captured screenshot and derives a target rectangle from explicit JSON edge
rules.  The first phase is diagnostic: callers can draw the result without
clicking anything.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from itertools import product
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageDraw

from .models import Point, Rect
from .template_matching import OpenCVTemplateMatcher, TemplateMatch


class RelativeLocatorConfigError(ValueError):
    """A locator JSON file is malformed or points to a missing asset."""


class RelativeLocatorError(RuntimeError):
    """A locator could not produce a safe target rectangle."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RelativeLocatorConfigError(f"locator JSON 不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RelativeLocatorConfigError(
            f"locator JSON 无效：{path}（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    if not isinstance(value, dict):
        raise RelativeLocatorConfigError(f"locator JSON 顶层必须是对象：{path}")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RelativeLocatorConfigError(f"{label} 必须是非空字符串")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RelativeLocatorConfigError(f"{label} 必须是数字")
    return float(value)


def _normalized_roi(value: Any, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise RelativeLocatorConfigError(f"{label} 必须是 [left, top, right, bottom]")
    result = tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))
    if not all(0.0 <= item <= 1.0 for item in result):
        raise RelativeLocatorConfigError(f"{label} 必须位于 0 到 1 之间")
    if result[0] >= result[2] or result[1] >= result[3]:
        raise RelativeLocatorConfigError(f"{label} 的左/上边必须小于右/下边")
    return result


def _absolute_roi(value: Any, label: str) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise RelativeLocatorConfigError(f"{label} 必须是 [left, top, right, bottom]")
    result: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise RelativeLocatorConfigError(f"{label}[{index}] 必须是非负整数")
        if item < 0:
            raise RelativeLocatorConfigError(f"{label}[{index}] 必须是非负整数")
        result.append(item)
    if result[0] >= result[2] or result[1] >= result[3]:
        raise RelativeLocatorConfigError(f"{label} 的左/上边必须小于右/下边")
    return result[0], result[1], result[2], result[3]


def _fraction_range(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise RelativeLocatorConfigError(f"{label} 必须是 [最小比例, 最大比例]")
    result = (_number(value[0], f"{label}[0]"), _number(value[1], f"{label}[1]"))
    if not 0.0 <= result[0] <= result[1] <= 1.0:
        raise RelativeLocatorConfigError(f"{label} 必须位于 0 到 1 之间并按升序排列")
    return result


@dataclass(frozen=True)
class AnchorTemplateSpec:
    path: Path
    minimum_score: float
    minimum_margin: float
    coarse_step: int
    scale_factors: tuple[float, ...] = (1.0,)
    native_scale: float = 1.0


@dataclass(frozen=True)
class AnchorSpec:
    anchor_id: str
    templates: tuple[AnchorTemplateSpec, ...]
    roi: tuple[float, float, float, float]
    pixel_roi: tuple[int, int, int, int] | None = None
    max_candidates: int = 8


# The review screen must be able to show a real control that ranked below
# runtime decoys (the emoji button in particular can be well below the first
# few matches on a scaled screenshot).  This is deliberately separate from
# ``AnchorSpec.max_candidates``: the latter is a performance guard for the
# live click path and must stay small.
DIAGNOSTIC_CANDIDATE_LIMIT = 32
MAX_REVIEW_CANDIDATES_PER_ANCHOR = 200


@dataclass(frozen=True)
class EdgeRule:
    anchor: str
    reference: str
    offset: float


@dataclass(frozen=True)
class OptionalAdjustmentSpec:
    """Modify one target edge only when an optional anchor is visible."""

    anchor: str
    edge: str
    reference: str
    offset: float
    mode: str


@dataclass(frozen=True)
class ScalarReferenceSpec:
    """One numeric edge/centre value from an anchor or the screenshot."""

    source: str
    reference: str


@dataclass(frozen=True)
class DifferenceConstraintSpec:
    """Accept only when ``left - right`` stays inside the configured range."""

    left: ScalarReferenceSpec
    right: ScalarReferenceSpec
    min_difference: float
    max_difference: float
    description: str = ""


@dataclass(frozen=True)
class AlternativeSpec:
    alternative_id: str
    anchors: tuple[str, ...]
    reference_edges: dict[str, EdgeRule] | None
    edges: dict[str, EdgeRule]
    optional_adjustments: tuple[OptionalAdjustmentSpec, ...]
    constraints: tuple[DifferenceConstraintSpec, ...]
    min_width: int
    max_width: int
    min_height: int
    max_height: int


@dataclass(frozen=True)
class RelativeLocatorSpec:
    source: Path
    locator_id: str
    theme: str
    anchors: dict[str, AnchorSpec]
    alternatives: tuple[AlternativeSpec, ...]
    click_x_range: tuple[float, float]
    click_y_range: tuple[float, float]


@dataclass(frozen=True)
class AnchorDetection:
    anchor_id: str
    template: Path | None
    bounds: Rect | None
    score: float
    second_score: float
    accepted: bool
    scale: float = 1.0

    @property
    def center(self) -> Point | None:
        return None if self.bounds is None else self.bounds.center


@dataclass(frozen=True)
class RelativeLocatorCombination:
    """One complete anchor combination that satisfies every locator rule."""

    combination_id: str
    alternative_id: str
    used_anchor_ids: tuple[str, ...]
    detections: dict[str, AnchorDetection]
    target: Rect
    click_bounds: Rect
    reference_bounds: Rect
    score: float


@dataclass(frozen=True)
class RelativeLocatorResult:
    alternative_id: str | None
    target: Rect | None
    detections: dict[str, AnchorDetection]
    rejected_alternatives: tuple[str, ...] = ()
    click_bounds: Rect | None = None
    reference_bounds: Rect | None = None
    anchor_candidates: dict[str, tuple[AnchorDetection, ...]] | None = None
    valid_combinations: tuple[RelativeLocatorCombination, ...] = ()
    distinct_combinations: tuple[RelativeLocatorCombination, ...] = ()
    failure_code: str = ""
    diagnostic_candidates: dict[str, tuple[AnchorDetection, ...]] | None = None

    @property
    def accepted(self) -> bool:
        return self.target is not None and self.alternative_id is not None


def _parse_template(source: Path, value: Any, label: str) -> AnchorTemplateSpec:
    if isinstance(value, str):
        data: dict[str, Any] = {"path": value}
    elif isinstance(value, dict):
        data = value
    else:
        raise RelativeLocatorConfigError(f"{label} 必须是路径字符串或对象")
    template_value = _non_empty_string(data.get("path"), f"{label}.path")
    path = (source.parent / template_value).resolve()
    if not path.is_file():
        raise RelativeLocatorConfigError(f"锚点模板不存在：{path}")
    minimum_score = _number(data.get("minimum_score", 0.90), f"{label}.minimum_score")
    minimum_margin = _number(data.get("minimum_margin", 0.01), f"{label}.minimum_margin")
    coarse_step_value = data.get("coarse_step", 1)
    if isinstance(coarse_step_value, bool) or not isinstance(coarse_step_value, int):
        raise RelativeLocatorConfigError(f"{label}.coarse_step 必须是正整数")
    if coarse_step_value < 1:
        raise RelativeLocatorConfigError(f"{label}.coarse_step 必须是正整数")
    if not 0.0 <= minimum_score <= 1.0 or minimum_margin < 0.0:
        raise RelativeLocatorConfigError(f"{label} 的匹配阈值无效")
    scale_values = data.get("scales", [1.0])
    if not isinstance(scale_values, list) or not scale_values:
        raise RelativeLocatorConfigError(f"{label}.scales 必须是非空数组")
    scales: list[float] = []
    for index, item in enumerate(scale_values):
        scale = _number(item, f"{label}.scales[{index}]")
        if not 0.5 <= scale <= 3.0:
            raise RelativeLocatorConfigError(
                f"{label}.scales[{index}] 必须位于 0.5 到 3.0 之间"
            )
        if scale not in scales:
            scales.append(scale)
    native_scale = _number(data.get("native_scale", 1.0), f"{label}.native_scale")
    if not 0.5 <= native_scale <= 3.0:
        raise RelativeLocatorConfigError(
            f"{label}.native_scale 必须位于 0.5 到 3.0 之间"
        )
    return AnchorTemplateSpec(
        path,
        minimum_score,
        minimum_margin,
        coarse_step_value,
        tuple(scales),
        native_scale,
    )


def _parse_scalar_reference(
    value: Any,
    label: str,
    required_anchors: tuple[str, ...],
) -> ScalarReferenceSpec:
    if not isinstance(value, dict):
        raise RelativeLocatorConfigError(f"{label} 必须是对象")
    source = _non_empty_string(value.get("source"), f"{label}.source")
    if source != "image" and source not in required_anchors:
        raise RelativeLocatorConfigError(
            f"{label}.source 必须是 image 或当前方案声明的锚点：{source}"
        )
    reference = _non_empty_string(value.get("reference"), f"{label}.reference")
    if reference not in {"left", "top", "right", "bottom", "center_x", "center_y"}:
        raise RelativeLocatorConfigError(f"{label}.reference 无效：{reference}")
    return ScalarReferenceSpec(source, reference)


def _local_calibration_root(source: Path) -> Path:
    return source.parent.parent / "local_calibration"


def local_constraint_adaptation_policy(
    constraint: DifferenceConstraintSpec,
) -> tuple[float, float]:
    """Return the maximum safe expansion and padding in logical pixels.

    A machine-local crop can end a few pixels inside the real control, so its
    centre or distance to the window edge may differ from the shipped crop.
    Window-edge relationships tolerate a little more crop variance than a
    relationship between two controls.  These limits are also enforced while
    loading the manifest, so editing the file cannot turn a local repair into
    an unbounded geometry rule.
    """

    sources = {constraint.left.source, constraint.right.source}
    if "image" in sources:
        references = {constraint.left.reference, constraint.right.reference}
        return (32.0, 5.0) if references & {"top", "bottom"} else (32.0, 8.0)
    return 12.0, 4.0


def adapt_constraint_to_observation(
    constraint: DifferenceConstraintSpec,
    observed_pixels: float,
    scale: float,
) -> tuple[DifferenceConstraintSpec | None, str, float]:
    """Safely include one human-selected observation in a local constraint.

    The JSON values are logical pixels and the locator applies display scale
    at runtime.  ``observed_pixels`` comes from the captured screenshot, so it
    is normalised before comparison and persistence.
    """

    if not math.isfinite(observed_pixels) or not math.isfinite(scale) or scale <= 0:
        return None, "failed", float("nan")
    observed = observed_pixels / scale
    minimum = constraint.min_difference
    maximum = constraint.max_difference
    if minimum <= observed <= maximum:
        return constraint, "passed", observed

    expansion, padding = local_constraint_adaptation_policy(constraint)
    outside = minimum - observed if observed < minimum else observed - maximum
    if outside > expansion:
        return None, "failed", observed

    adjusted_minimum = minimum
    adjusted_maximum = maximum
    if observed < minimum:
        adjusted_minimum = max(minimum - expansion, math.floor(observed - padding))
    else:
        adjusted_maximum = min(maximum + expansion, math.ceil(observed + padding))
    return (
        replace(
            constraint,
            min_difference=float(adjusted_minimum),
            max_difference=float(adjusted_maximum),
        ),
        "adapted",
        observed,
    )


def _reference_payload(value: ScalarReferenceSpec) -> dict[str, str]:
    return {"source": value.source, "reference": value.reference}


def _apply_local_geometry(
    alternatives: tuple[AlternativeSpec, ...],
    profile: dict[str, Any],
) -> tuple[AlternativeSpec, ...]:
    """Apply a validated local geometry overlay atomically and defensively."""

    geometry = profile.get("geometry")
    if geometry is None:
        return alternatives
    if not isinstance(geometry, dict) or geometry.get("version") != 1:
        return alternatives
    alternative_id = str(geometry.get("alternative_id") or "")
    if not alternative_id or alternative_id != str(profile.get("alternative_id") or ""):
        return alternatives
    alternative_index = next(
        (
            index
            for index, value in enumerate(alternatives)
            if value.alternative_id == alternative_id
        ),
        None,
    )
    if alternative_index is None:
        return alternatives
    entries = geometry.get("constraints")
    if not isinstance(entries, list) or not entries:
        return alternatives

    alternative = alternatives[alternative_index]
    constraints = list(alternative.constraints)
    seen: set[int] = set()
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                return alternatives
            index = entry.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                return alternatives
            if index in seen or not 0 <= index < len(constraints):
                return alternatives
            seen.add(index)
            original = constraints[index]
            if entry.get("left") != _reference_payload(original.left):
                return alternatives
            if entry.get("right") != _reference_payload(original.right):
                return alternatives
            minimum = float(entry.get("min_difference"))
            maximum = float(entry.get("max_difference"))
            if not math.isfinite(minimum) or not math.isfinite(maximum):
                return alternatives
            expansion, _padding = local_constraint_adaptation_policy(original)
            if (
                minimum > original.min_difference
                or maximum < original.max_difference
                or minimum < original.min_difference - expansion
                or maximum > original.max_difference + expansion
                or minimum > maximum
            ):
                return alternatives
            constraints[index] = replace(
                original,
                min_difference=minimum,
                max_difference=maximum,
            )
    except (TypeError, ValueError, OverflowError):
        return alternatives

    updated = list(alternatives)
    updated[alternative_index] = replace(
        alternative,
        constraints=tuple(constraints),
    )
    return tuple(updated)


def apply_local_calibration(
    spec: RelativeLocatorSpec,
    calibration_root: str | Path | None = None,
) -> RelativeLocatorSpec:
    """Prepend validated machine-local templates without editing shipped JSON."""

    root = Path(calibration_root or _local_calibration_root(spec.source)).resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        profile = dict((manifest.get("profiles") or {}).get(spec.locator_id) or {})
    except (OSError, ValueError, TypeError, AttributeError):
        return spec
    if manifest.get("version") != 1 or not profile.get("active"):
        return spec

    anchors = dict(spec.anchors)
    try:
        for anchor_id, values in dict(profile.get("anchors") or {}).items():
            if anchor_id not in anchors or not isinstance(values, list):
                continue
            custom: list[AnchorTemplateSpec] = []
            for value in values:
                if not isinstance(value, dict):
                    continue
                template_path = (root / Path(str(value.get("path") or ""))).resolve()
                try:
                    template_path.relative_to(root)
                except ValueError:
                    continue
                if not template_path.is_file():
                    continue
                native_scale = float(value.get("native_scale", 1.0))
                if not 0.5 <= native_scale <= 3.0:
                    continue
                custom.append(
                    AnchorTemplateSpec(
                        path=template_path,
                        minimum_score=float(value.get("minimum_score", 0.97)),
                        minimum_margin=float(value.get("minimum_margin", 0.02)),
                        coarse_step=max(1, int(value.get("coarse_step", 1))),
                        scale_factors=(native_scale,),
                        native_scale=native_scale,
                    )
                )
            if custom:
                anchors[anchor_id] = replace(
                    anchors[anchor_id],
                    templates=tuple(custom) + anchors[anchor_id].templates,
                )
    except (TypeError, ValueError, OSError):
        return spec
    alternatives = _apply_local_geometry(spec.alternatives, profile)
    return replace(spec, anchors=anchors, alternatives=alternatives)


def load_relative_locator(
    path: str | Path,
    *,
    calibration_root: str | Path | None = None,
    include_local: bool = True,
) -> RelativeLocatorSpec:
    source = Path(path).resolve()
    data = _object(source)
    if data.get("version", 1) != 1:
        raise RelativeLocatorConfigError(f"不支持的 locator JSON 版本：{data.get('version')}")
    locator_id = _non_empty_string(data.get("id", source.stem), "locator.id")
    theme = _non_empty_string(data.get("theme", "light"), "locator.theme")
    if theme != "light":
        raise RelativeLocatorConfigError(
            f"当前 v3 自动化只支持 light 主题定位规则，不能加载：{theme}"
        )
    anchor_values = data.get("anchors")
    if not isinstance(anchor_values, dict) or not anchor_values:
        raise RelativeLocatorConfigError("locator.anchors 必须是非空对象")
    anchors: dict[str, AnchorSpec] = {}
    for anchor_id, value in anchor_values.items():
        anchor_id = _non_empty_string(anchor_id, "anchor id")
        if not isinstance(value, dict):
            raise RelativeLocatorConfigError(f"anchors.{anchor_id} 必须是对象")
        templates = value.get("templates")
        if not isinstance(templates, list) or not templates:
            raise RelativeLocatorConfigError(f"anchors.{anchor_id}.templates 不能为空")
        anchors[anchor_id] = AnchorSpec(
            anchor_id=anchor_id,
            templates=tuple(
                _parse_template(source, item, f"anchors.{anchor_id}.templates[{index}]")
                for index, item in enumerate(templates)
            ),
            roi=_normalized_roi(value.get("roi", [0, 0, 1, 1]), f"anchors.{anchor_id}.roi"),
            pixel_roi=_absolute_roi(
                value.get("pixel_roi"),
                f"anchors.{anchor_id}.pixel_roi",
            ),
            max_candidates=int(
                _number(
                    value.get("max_candidates", 8),
                    f"anchors.{anchor_id}.max_candidates",
                )
            ),
        )
        if not 1 <= anchors[anchor_id].max_candidates <= 32:
            raise RelativeLocatorConfigError(
                f"anchors.{anchor_id}.max_candidates 必须在 1 到 32 之间"
            )

    alternative_values = data.get("alternatives")
    if not isinstance(alternative_values, list) or not alternative_values:
        raise RelativeLocatorConfigError("locator.alternatives 必须是非空数组")
    alternatives: list[AlternativeSpec] = []
    for index, value in enumerate(alternative_values):
        label = f"alternatives[{index}]"
        if not isinstance(value, dict):
            raise RelativeLocatorConfigError(f"{label} 必须是对象")
        alternative_id = _non_empty_string(value.get("id", f"alternative_{index + 1}"), f"{label}.id")
        required = value.get("anchors")
        if not isinstance(required, list) or not required:
            raise RelativeLocatorConfigError(f"{label}.anchors 必须是非空数组")
        required_ids = tuple(_non_empty_string(item, f"{label}.anchors[{i}]") for i, item in enumerate(required))
        unknown = [item for item in required_ids if item not in anchors]
        if unknown:
            raise RelativeLocatorConfigError(f"{label} 引用了未知锚点：{unknown}")
        reference_target = value.get("reference_target")
        reference_edges: dict[str, EdgeRule] | None = None
        if reference_target is not None:
            if not isinstance(reference_target, dict):
                raise RelativeLocatorConfigError(f"{label}.reference_target 必须是对象")
            reference_edges = {}
            for edge in ("left", "top", "right", "bottom"):
                rule = reference_target.get(edge)
                if not isinstance(rule, dict):
                    raise RelativeLocatorConfigError(f"{label}.reference_target.{edge} 必须是对象")
                anchor = _non_empty_string(rule.get("anchor"), f"{label}.reference_target.{edge}.anchor")
                if anchor not in required_ids:
                    raise RelativeLocatorConfigError(
                        f"{label}.reference_target.{edge} 未声明所需锚点：{anchor}"
                    )
                reference = _non_empty_string(
                    rule.get("reference"),
                    f"{label}.reference_target.{edge}.reference",
                )
                if reference not in {"left", "top", "right", "bottom", "center_x", "center_y"}:
                    raise RelativeLocatorConfigError(
                        f"{label}.reference_target.{edge}.reference 无效：{reference}"
                    )
                reference_edges[edge] = EdgeRule(
                    anchor,
                    reference,
                    _number(rule.get("offset", 0), f"{label}.reference_target.{edge}.offset"),
                )
        target = value.get("target")
        if not isinstance(target, dict):
            raise RelativeLocatorConfigError(f"{label}.target 必须是对象")
        edges: dict[str, EdgeRule] = {}
        for edge in ("left", "top", "right", "bottom"):
            rule = target.get(edge)
            if not isinstance(rule, dict):
                raise RelativeLocatorConfigError(f"{label}.target.{edge} 必须是对象")
            anchor = _non_empty_string(rule.get("anchor"), f"{label}.target.{edge}.anchor")
            if anchor not in required_ids:
                raise RelativeLocatorConfigError(f"{label}.target.{edge} 未声明所需锚点：{anchor}")
            reference = _non_empty_string(rule.get("reference"), f"{label}.target.{edge}.reference")
            if reference not in {"left", "top", "right", "bottom", "center_x", "center_y"}:
                raise RelativeLocatorConfigError(f"{label}.target.{edge}.reference 无效：{reference}")
            edges[edge] = EdgeRule(anchor, reference, _number(rule.get("offset", 0), f"{label}.target.{edge}.offset"))
        adjustment_values = value.get("optional_adjustments", [])
        if not isinstance(adjustment_values, list):
            raise RelativeLocatorConfigError(f"{label}.optional_adjustments 必须是数组")
        optional_adjustments: list[OptionalAdjustmentSpec] = []
        for adjustment_index, adjustment in enumerate(adjustment_values):
            adjustment_label = f"{label}.optional_adjustments[{adjustment_index}]"
            if not isinstance(adjustment, dict):
                raise RelativeLocatorConfigError(f"{adjustment_label} 必须是对象")
            anchor = _non_empty_string(adjustment.get("anchor"), f"{adjustment_label}.anchor")
            if anchor not in anchors:
                raise RelativeLocatorConfigError(f"{adjustment_label} 引用了未知锚点：{anchor}")
            edge = _non_empty_string(adjustment.get("edge"), f"{adjustment_label}.edge")
            if edge not in {"left", "top", "right", "bottom"}:
                raise RelativeLocatorConfigError(f"{adjustment_label}.edge 无效：{edge}")
            reference = _non_empty_string(adjustment.get("reference"), f"{adjustment_label}.reference")
            if reference not in {"left", "top", "right", "bottom", "center_x", "center_y"}:
                raise RelativeLocatorConfigError(f"{adjustment_label}.reference 无效：{reference}")
            mode = _non_empty_string(adjustment.get("mode", "replace"), f"{adjustment_label}.mode")
            if mode not in {"min", "max", "replace"}:
                raise RelativeLocatorConfigError(f"{adjustment_label}.mode 只能是 min、max 或 replace")
            optional_adjustments.append(OptionalAdjustmentSpec(
                anchor=anchor,
                edge=edge,
                reference=reference,
                offset=_number(adjustment.get("offset", 0), f"{adjustment_label}.offset"),
                mode=mode,
            ))
        constraint_values = value.get("constraints", [])
        if not isinstance(constraint_values, list):
            raise RelativeLocatorConfigError(f"{label}.constraints 必须是数组")
        constraints: list[DifferenceConstraintSpec] = []
        for constraint_index, constraint in enumerate(constraint_values):
            constraint_label = f"{label}.constraints[{constraint_index}]"
            if not isinstance(constraint, dict):
                raise RelativeLocatorConfigError(f"{constraint_label} 必须是对象")
            min_difference = _number(
                constraint.get("min_difference"),
                f"{constraint_label}.min_difference",
            )
            max_difference = _number(
                constraint.get("max_difference"),
                f"{constraint_label}.max_difference",
            )
            if min_difference > max_difference:
                raise RelativeLocatorConfigError(
                    f"{constraint_label} 的最小差值不能大于最大差值"
                )
            constraints.append(DifferenceConstraintSpec(
                left=_parse_scalar_reference(
                    constraint.get("left"),
                    f"{constraint_label}.left",
                    required_ids,
                ),
                right=_parse_scalar_reference(
                    constraint.get("right"),
                    f"{constraint_label}.right",
                    required_ids,
                ),
                min_difference=min_difference,
                max_difference=max_difference,
                description=str(constraint.get("description") or "").strip(),
            ))
        bounds = value.get("bounds", {})
        if not isinstance(bounds, dict):
            raise RelativeLocatorConfigError(f"{label}.bounds 必须是对象")
        min_width = int(_number(bounds.get("min_width", 20), f"{label}.bounds.min_width"))
        max_width = int(_number(bounds.get("max_width", 1200), f"{label}.bounds.max_width"))
        min_height = int(_number(bounds.get("min_height", 10), f"{label}.bounds.min_height"))
        max_height = int(_number(bounds.get("max_height", 300), f"{label}.bounds.max_height"))
        if not (0 < min_width <= max_width and 0 < min_height <= max_height):
            raise RelativeLocatorConfigError(f"{label}.bounds 范围无效")
        alternatives.append(AlternativeSpec(
            alternative_id,
            required_ids,
            reference_edges,
            edges,
            tuple(optional_adjustments),
            tuple(constraints),
            min_width,
            max_width,
            min_height,
            max_height,
        ))
    click = data.get("click", {})
    if not isinstance(click, dict):
        raise RelativeLocatorConfigError("locator.click 必须是对象")
    click_x_range = _fraction_range(click.get("x_fraction", [0.18, 0.82]), "locator.click.x_fraction")
    click_y_range = _fraction_range(click.get("y_fraction", [0.40, 0.60]), "locator.click.y_fraction")
    spec = RelativeLocatorSpec(
        source,
        locator_id,
        theme,
        anchors,
        tuple(alternatives),
        click_x_range,
        click_y_range,
    )
    return apply_local_calibration(spec, calibration_root) if include_local else spec


def _pixel_roi(
    image: Image.Image,
    roi: tuple[float, float, float, float],
    absolute: tuple[int, int, int, int] | None = None,
    scales: tuple[float, ...] = (1.0,),
) -> tuple[int, int, int, int]:
    width, height = image.size
    if absolute is not None:
        left, top, right, bottom = absolute
        minimum_scale = min(scales)
        maximum_scale = max(scales)
        left = int(left * minimum_scale)
        top = int(top * minimum_scale)
        right = round(right * maximum_scale)
        bottom = round(bottom * maximum_scale)
        return (
            min(width, left),
            min(height, top),
            min(width, max(right, left + 1)),
            min(height, max(bottom, top + 1)),
        )
    left, top, right, bottom = roi
    return (
        int(width * left),
        int(height * top),
        max(int(width * right), int(width * left) + 1),
        max(int(height * bottom), int(height * top) + 1),
    )


def _reference(bounds: Rect, reference: str) -> float:
    if reference == "left":
        return float(bounds.left)
    if reference == "top":
        return float(bounds.top)
    if reference == "right":
        return float(bounds.right)
    if reference == "bottom":
        return float(bounds.bottom)
    if reference == "center_x":
        return float(bounds.left + bounds.width // 2)
    if reference == "center_y":
        return float(bounds.top + bounds.height // 2)
    raise AssertionError(reference)


def _image_reference(image: Image.Image, reference: str) -> float:
    if reference in {"left", "top"}:
        return 0.0
    if reference == "right":
        return float(image.width)
    if reference == "bottom":
        return float(image.height)
    if reference == "center_x":
        return float(image.width // 2)
    if reference == "center_y":
        return float(image.height // 2)
    raise AssertionError(reference)


class RelativeLocator:
    """Match configured anchors and derive a target rectangle."""

    def __init__(self, spec: RelativeLocatorSpec):
        self.spec = spec
        self._preferred_scale_factors: tuple[float, ...] | None = None
        self._fallback_scale_factors: tuple[float, ...] | None = None
        self._matchers: dict[
            tuple[str, float, float, int, tuple[float, ...]],
            OpenCVTemplateMatcher,
        ] = {}

    @staticmethod
    def _normalise_scale_factors(values: tuple[float, ...] | list[float]) -> tuple[float, ...]:
        scales: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("DPI 缩放比例必须是数字。")
            scale = round(float(value), 4)
            if not 0.5 <= scale <= 3.0:
                raise ValueError("DPI 缩放比例必须位于 0.5 到 3.0 之间。")
            if scale not in scales:
                scales.append(scale)
        if not scales:
            raise ValueError("DPI 缩放比例不能为空。")
        return tuple(scales)

    def set_scale_policy(
        self,
        preferred: tuple[float, ...] | list[float],
        fallback: tuple[float, ...] | list[float] | None = None,
    ) -> None:
        """Try the reported/manual scale first, then a configured auto range."""

        primary = self._normalise_scale_factors(preferred)
        secondary = self._normalise_scale_factors(fallback or primary)
        self._preferred_scale_factors = primary
        self._fallback_scale_factors = secondary

    def _scale_attempts(self) -> tuple[tuple[float, ...] | None, ...]:
        """Return one display DPI per runtime attempt, in priority order.

        A locator combination must be evaluated at one coherent display scale.
        Mixing the entire automatic scan range into a single matcher call lets
        anchors from different DPI hypotheses compete with each other and also
        defeats native-DPI template priority.
        """

        if self._preferred_scale_factors is None:
            return (None,)
        ordered = list(self._preferred_scale_factors)
        for scale in self._fallback_scale_factors or ():
            if scale not in ordered:
                ordered.append(scale)
        return tuple((scale,) for scale in ordered)

    @staticmethod
    def _runtime_template_tiers(
        anchor: AnchorSpec,
        scale_factors: tuple[float, ...] | None,
    ) -> tuple[tuple[AnchorTemplateSpec, ...], ...]:
        """Prefer templates captured natively at the current display DPI.

        Runtime matching only falls back to templates captured at another DPI
        when every native-DPI template is below its own threshold. Diagnostics
        deliberately do not use these tiers because the repair screen needs a
        complete picture of all templates and near misses.
        """

        if scale_factors is None or len(scale_factors) != 1:
            return (anchor.templates,)
        display_scale = scale_factors[0]
        native = tuple(
            template
            for template in anchor.templates
            if math.isclose(template.native_scale, display_scale, abs_tol=0.0001)
        )
        other = tuple(template for template in anchor.templates if template not in native)
        if native and other:
            return native, other
        return (native or other,)

    def _matcher_for(
        self,
        template: AnchorTemplateSpec,
        scale_factors: tuple[float, ...] | None = None,
    ) -> OpenCVTemplateMatcher:
        display_scales = scale_factors or template.scale_factors
        scales = tuple(
            round(scale / template.native_scale, 6) for scale in display_scales
        )
        key = (
            str(template.path),
            template.minimum_score,
            template.minimum_margin,
            template.coarse_step,
            scales,
        )
        matcher = self._matchers.get(key)
        if matcher is None:
            matcher = OpenCVTemplateMatcher(
                template.path,
                coarse_step=template.coarse_step,
                minimum_score=template.minimum_score,
                minimum_margin=template.minimum_margin,
                scale_factors=scales,
            )
            self._matchers[key] = matcher
        return matcher

    def _match_template(
        self,
        image: Image.Image,
        template: AnchorTemplateSpec,
        roi: tuple[int, int, int, int],
        scale_factors: tuple[float, ...] | None = None,
    ) -> TemplateMatch:
        result = self._matcher_for(template, scale_factors).match(image, roi=roi)
        return TemplateMatch(
            result.best_bounds,
            result.score,
            result.second_score,
            result.accepted,
            result.scale * template.native_scale,
        )

    @staticmethod
    def _missing_detection(anchor_id: str) -> AnchorDetection:
        return AnchorDetection(anchor_id, None, None, 0.0, 0.0, False)

    @staticmethod
    def _same_anchor_hit(left: AnchorDetection, right: AnchorDetection) -> bool:
        if left.bounds is None or right.bounds is None:
            return False
        left_center = left.bounds.center
        right_center = right.bounds.center
        return (
            abs(left_center.x - right_center.x)
            <= max(3, min(left.bounds.width, right.bounds.width) // 3)
            and abs(left_center.y - right_center.y)
            <= max(3, min(left.bounds.height, right.bounds.height) // 3)
        )

    def _candidates_for_anchor(
        self,
        image: Image.Image,
        anchor: AnchorSpec,
        roi: tuple[int, int, int, int],
        scale_factors: tuple[float, ...] | None = None,
    ) -> tuple[AnchorDetection, ...]:
        found: list[AnchorDetection] = []
        for template_tier in self._runtime_template_tiers(anchor, scale_factors):
            tier_found: list[AnchorDetection] = []
            for template in template_tier:
                matcher = self._matcher_for(template, scale_factors)
                for candidate in matcher.find_candidates(
                    image,
                    roi=roi,
                    max_candidates=anchor.max_candidates,
                ):
                    detection = AnchorDetection(
                        anchor.anchor_id,
                        template.path,
                        candidate.bounds,
                        candidate.score,
                        0.0,
                        True,
                        candidate.scale * template.native_scale,
                    )
                    duplicate = next(
                        (
                            index
                            for index, existing in enumerate(tier_found)
                            if self._same_anchor_hit(existing, detection)
                        ),
                        None,
                    )
                    if duplicate is None:
                        tier_found.append(detection)
                    elif detection.score > tier_found[duplicate].score:
                        tier_found[duplicate] = detection
            if tier_found:
                found = tier_found
                break
        found.sort(key=lambda item: item.score, reverse=True)
        limited = found[: anchor.max_candidates]
        return tuple(
            AnchorDetection(
                item.anchor_id,
                item.template,
                item.bounds,
                item.score,
                max(
                    (
                        other.score
                        for other in limited
                        if other is not item and not self._same_anchor_hit(item, other)
                    ),
                    default=0.0,
                ),
                True,
                item.scale,
            )
            for item in limited
        )

    def _diagnostic_candidates_for_anchor(
        self,
        image: Image.Image,
        anchor: AnchorSpec,
        roi: tuple[int, int, int, int],
        scale_factors: tuple[float, ...] | None = None,
        *,
        max_candidates: int = DIAGNOSTIC_CANDIDATE_LIMIT,
        score_floor: float = 0.0,
    ) -> tuple[AnchorDetection, ...]:
        """Collect best-effort spatial hits while preserving acceptance state."""

        # Diagnostics must not reuse the small runtime candidate budget.  The
        # runtime path intentionally keeps this low for speed, while the
        # repair UI needs enough near-miss crops for a human to identify an
        # icon that ranked below the first few false positives.
        diagnostic_limit = max(anchor.max_candidates, int(max_candidates))
        found: list[AnchorDetection] = []
        for template in anchor.templates:
            matcher = self._matcher_for(template, scale_factors)
            for candidate in matcher.find_diagnostic_candidates(
                image,
                roi=roi,
                max_candidates=diagnostic_limit,
                score_floor=score_floor,
            ):
                detection = AnchorDetection(
                    anchor.anchor_id,
                    template.path,
                    candidate.bounds,
                    candidate.score,
                    0.0,
                    candidate.score >= template.minimum_score,
                    candidate.scale * template.native_scale,
                )
                duplicate = next(
                    (
                        index
                        for index, existing in enumerate(found)
                        if self._same_anchor_hit(existing, detection)
                    ),
                    None,
                )
                if duplicate is None:
                    found.append(detection)
                elif detection.score > found[duplicate].score:
                    found[duplicate] = detection
        found.sort(key=lambda item: item.score, reverse=True)
        limited = found[:diagnostic_limit]
        return tuple(
            replace(
                item,
                second_score=max(
                    (
                        other.score
                        for other in limited
                        if other is not item and not self._same_anchor_hit(item, other)
                    ),
                    default=0.0,
                ),
            )
            for item in limited
        )

    def detect_anchor_candidates(
        self,
        image: Image.Image,
        *,
        skip_anchors: set[str] | None = None,
        roi_overrides: dict[str, tuple[int, int, int, int]] | None = None,
        scale_factors: tuple[float, ...] | None = None,
    ) -> dict[str, tuple[AnchorDetection, ...]]:
        skip_anchors = set(skip_anchors or ())
        roi_overrides = dict(roi_overrides or {})
        candidates: dict[str, tuple[AnchorDetection, ...]] = {}
        for anchor_id, anchor in self.spec.anchors.items():
            if anchor_id in skip_anchors:
                candidates[anchor_id] = ()
                continue
            roi = roi_overrides.get(
                anchor_id,
                _pixel_roi(
                    image,
                    anchor.roi,
                    anchor.pixel_roi,
                    tuple(
                        scale
                        for template in anchor.templates
                        for scale in (scale_factors or template.scale_factors)
                    ),
                ),
            )
            candidates[anchor_id] = self._candidates_for_anchor(
                image,
                anchor,
                roi,
                scale_factors,
            )
        return candidates

    def detect_diagnostic_candidates(
        self,
        image: Image.Image,
        *,
        skip_anchors: set[str] | None = None,
        scale_factors: tuple[float, ...] | None = None,
        max_candidates: int = DIAGNOSTIC_CANDIDATE_LIMIT,
        score_floor: float = 0.0,
    ) -> dict[str, tuple[AnchorDetection, ...]]:
        """Return reviewable near-miss candidates for a captured screenshot."""

        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise ValueError("诊断候选数量上限必须是整数。")
        if not 1 <= max_candidates <= MAX_REVIEW_CANDIDATES_PER_ANCHOR:
            raise ValueError(
                "诊断候选数量上限必须位于 1 到 "
                f"{MAX_REVIEW_CANDIDATES_PER_ANCHOR} 之间。"
            )
        if isinstance(score_floor, bool) or not isinstance(score_floor, (int, float)):
            raise ValueError("诊断候选最低匹配分数必须是数字。")
        score_floor = float(score_floor)
        if not 0.0 <= score_floor <= 1.0:
            raise ValueError("诊断候选最低匹配分数必须位于 0 到 1 之间。")
        skip_anchors = set(skip_anchors or ())
        candidates: dict[str, tuple[AnchorDetection, ...]] = {}
        for anchor_id, anchor in self.spec.anchors.items():
            if anchor_id in skip_anchors:
                candidates[anchor_id] = ()
                continue
            roi = _pixel_roi(
                image,
                anchor.roi,
                anchor.pixel_roi,
                tuple(
                    scale
                    for template in anchor.templates
                    for scale in (scale_factors or template.scale_factors)
                ),
            )
            candidates[anchor_id] = self._diagnostic_candidates_for_anchor(
                image,
                anchor,
                roi,
                scale_factors,
                max_candidates=max_candidates,
                score_floor=score_floor,
            )
        return candidates

    def detect_anchors(
        self,
        image: Image.Image,
        *,
        skip_anchors: set[str] | None = None,
    ) -> dict[str, AnchorDetection]:
        candidates = self.detect_anchor_candidates(
            image,
            skip_anchors=skip_anchors,
        )
        return {
            anchor_id: values[0] if values else self._missing_detection(anchor_id)
            for anchor_id, values in candidates.items()
        }

    @staticmethod
    def _constraint_value(
        value: ScalarReferenceSpec,
        detections: dict[str, AnchorDetection],
        image: Image.Image,
    ) -> float | None:
        if value.source == "image":
            return _image_reference(image, value.reference)
        detection = detections[value.source]
        if not detection.accepted or detection.bounds is None:
            return None
        return _reference(detection.bounds, value.reference)

    @staticmethod
    def _combination_scale(
        alternative: AlternativeSpec,
        detections: dict[str, AnchorDetection],
    ) -> float | None:
        scales = [detections[name].scale for name in alternative.anchors]
        if not scales:
            return 1.0
        typical = float(median(scales))
        tolerance = max(0.08, typical * 0.08)
        if max(scales) - min(scales) > tolerance:
            return None
        return typical

    def _constraints_hold(
        self,
        alternative: AlternativeSpec,
        detections: dict[str, AnchorDetection],
        image: Image.Image,
        scale: float,
    ) -> bool:
        for constraint in alternative.constraints:
            left = self._constraint_value(constraint.left, detections, image)
            right = self._constraint_value(constraint.right, detections, image)
            if left is None or right is None:
                return False
            difference = left - right
            if not (
                constraint.min_difference * scale
                <= difference
                <= constraint.max_difference * scale
            ):
                return False
        return True

    def _derive_target(self, alternative: AlternativeSpec, detections: dict[str, AnchorDetection], image: Image.Image) -> Rect | None:
        if any(not detections[name].accepted or detections[name].bounds is None for name in alternative.anchors):
            return None
        scale = self._combination_scale(alternative, detections)
        if scale is None or not self._constraints_hold(
            alternative,
            detections,
            image,
            scale,
        ):
            return None
        values: dict[str, int] = {}
        for edge, rule in alternative.edges.items():
            bounds = detections[rule.anchor].bounds
            assert bounds is not None
            values[edge] = round(
                _reference(bounds, rule.reference) + rule.offset * scale
            )
        for adjustment in alternative.optional_adjustments:
            detection = detections[adjustment.anchor]
            if not detection.accepted or detection.bounds is None:
                continue
            candidate = round(
                _reference(detection.bounds, adjustment.reference)
                + adjustment.offset * scale
            )
            if adjustment.mode == "min":
                values[adjustment.edge] = min(values[adjustment.edge], candidate)
            elif adjustment.mode == "max":
                values[adjustment.edge] = max(values[adjustment.edge], candidate)
            else:
                values[adjustment.edge] = candidate
        try:
            target = Rect(values["left"], values["top"], values["right"], values["bottom"])
        except ValueError:
            return None
        if not (
            alternative.min_width * scale
            <= target.width
            <= alternative.max_width * scale
            and alternative.min_height * scale
            <= target.height
            <= alternative.max_height * scale
        ):
            return None
        if not (0 <= target.left < image.width and 0 < target.right <= image.width and 0 <= target.top < image.height and 0 < target.bottom <= image.height):
            return None
        return target

    @staticmethod
    def _derive_reference(
        alternative: AlternativeSpec,
        detections: dict[str, AnchorDetection],
        image: Image.Image,
    ) -> Rect | None:
        if alternative.reference_edges is None:
            return None
        if any(
            not detections[name].accepted or detections[name].bounds is None
            for name in alternative.anchors
        ):
            return None
        scale = RelativeLocator._combination_scale(alternative, detections)
        if scale is None:
            return None
        values: dict[str, int] = {}
        for edge, rule in alternative.reference_edges.items():
            bounds = detections[rule.anchor].bounds
            assert bounds is not None
            values[edge] = round(
                _reference(bounds, rule.reference) + rule.offset * scale
            )
        try:
            reference = Rect(
                values["left"],
                values["top"],
                values["right"],
                values["bottom"],
            )
        except ValueError:
            return None
        if not (
            0 <= reference.left < image.width
            and 0 < reference.right <= image.width
            and 0 <= reference.top < image.height
            and 0 < reference.bottom <= image.height
        ):
            return None
        return reference

    def _click_bounds(self, target: Rect) -> Rect:
        x_min, x_max = self.spec.click_x_range
        y_min, y_max = self.spec.click_y_range
        left = target.left + int(target.width * x_min)
        right = target.left + max(int(target.width * x_max), int(target.width * x_min) + 1)
        top = target.top + int(target.height * y_min)
        bottom = target.top + max(int(target.height * y_max), int(target.height * y_min) + 1)
        return Rect(
            max(target.left, left),
            max(target.top, top),
            min(target.right, right),
            min(target.bottom, bottom),
        )

    @staticmethod
    def _same_target(
        left: RelativeLocatorCombination,
        right: RelativeLocatorCombination,
    ) -> bool:
        return all(
            abs(a - b) <= 3
            for a, b in (
                (left.target.left, right.target.left),
                (left.target.top, right.target.top),
                (left.target.right, right.target.right),
                (left.target.bottom, right.target.bottom),
            )
        )

    def _result_from_candidates(
        self,
        image: Image.Image,
        anchor_candidates: dict[str, tuple[AnchorDetection, ...]],
        *,
        alternatives: tuple[AlternativeSpec, ...] | None = None,
    ) -> RelativeLocatorResult:
        selected_alternatives = alternatives or self.spec.alternatives
        best_detections = {
            anchor_id: values[0] if values else self._missing_detection(anchor_id)
            for anchor_id, values in anchor_candidates.items()
        }
        rejected: list[str] = []
        valid: list[RelativeLocatorCombination] = []
        alternatives_with_complete_candidates = 0
        combination_number = 0
        for alternative in selected_alternatives:
            required_lists = [anchor_candidates.get(name, ()) for name in alternative.anchors]
            if any(not values for values in required_lists):
                rejected.append(alternative.alternative_id)
                continue
            alternatives_with_complete_candidates += 1
            optional_ids = tuple(
                dict.fromkeys(item.anchor for item in alternative.optional_adjustments)
            )
            optional_lists: list[tuple[AnchorDetection, ...]] = []
            for anchor_id in optional_ids:
                values = anchor_candidates.get(anchor_id, ())
                optional_lists.append(
                    values if values else (self._missing_detection(anchor_id),)
                )
            alternative_valid = 0
            for chosen in product(*(required_lists + optional_lists)):
                detections = dict(best_detections)
                for anchor_id, detection in zip(
                    alternative.anchors + optional_ids,
                    chosen,
                ):
                    detections[anchor_id] = detection
                target = self._derive_target(alternative, detections, image)
                if target is None:
                    continue
                reference = self._derive_reference(alternative, detections, image) or target
                accepted_detections = [
                    detections[name]
                    for name in alternative.anchors + optional_ids
                    if detections[name].accepted
                ]
                combination_number += 1
                alternative_valid += 1
                valid.append(
                    RelativeLocatorCombination(
                        combination_id=f"combination_{combination_number}",
                        alternative_id=alternative.alternative_id,
                        used_anchor_ids=alternative.anchors + tuple(
                            anchor_id
                            for anchor_id in optional_ids
                            if detections[anchor_id].accepted
                        ),
                        detections=detections,
                        target=target,
                        click_bounds=self._click_bounds(target),
                        reference_bounds=reference,
                        score=(
                            sum(item.score for item in accepted_detections)
                            / len(accepted_detections)
                            if accepted_detections
                            else 0.0
                        ),
                    )
                )
            if not alternative_valid:
                rejected.append(alternative.alternative_id)

        if not valid:
            return RelativeLocatorResult(
                None,
                None,
                best_detections,
                tuple(rejected),
                anchor_candidates=anchor_candidates,
                failure_code=(
                    "anchor_candidates_missing"
                    if alternatives_with_complete_candidates == 0
                    else "no_valid_combination"
                ),
            )

        target_groups: list[list[RelativeLocatorCombination]] = []
        for combination in valid:
            group = next(
                (
                    items
                    for items in target_groups
                    if self._same_target(items[0], combination)
                ),
                None,
            )
            if group is None:
                target_groups.append([combination])
            else:
                group.append(combination)
        alternative_order = {
            item.alternative_id: index
            for index, item in enumerate(selected_alternatives)
        }
        representatives = tuple(
            max(items, key=lambda item: item.score)
            for items in target_groups
        )
        if len(target_groups) > 1:
            return RelativeLocatorResult(
                None,
                None,
                best_detections,
                tuple(rejected),
                anchor_candidates=anchor_candidates,
                valid_combinations=tuple(valid),
                distinct_combinations=representatives,
                failure_code="ambiguous_combinations",
            )

        chosen = min(
            target_groups[0],
            key=lambda item: (
                alternative_order.get(item.alternative_id, 999),
                -item.score,
            ),
        )
        return RelativeLocatorResult(
            chosen.alternative_id,
            chosen.target,
            chosen.detections,
            tuple(rejected),
            chosen.click_bounds,
            chosen.reference_bounds,
            anchor_candidates,
            tuple(valid),
            representatives,
            "",
        )

    def _result_from_detections(
        self,
        image: Image.Image,
        detections: dict[str, AnchorDetection],
        *,
        alternatives: tuple[AlternativeSpec, ...] | None = None,
    ) -> RelativeLocatorResult:
        return self._result_from_candidates(
            image,
            {
                anchor_id: (detection,) if detection.accepted else ()
                for anchor_id, detection in detections.items()
            },
            alternatives=alternatives,
        )

    def locate_near(
        self,
        image: Image.Image,
        cached: RelativeLocatorResult,
        *,
        padding: int = 10,
        skip_optional_anchors: bool = False,
    ) -> RelativeLocatorResult:
        """Revalidate cached anchors only inside small surrounding rectangles.

        A failed local check is deliberately returned as a rejected result;
        callers must then run ``locate`` on the same screenshot before using a
        click rectangle.
        """

        if padding < 0:
            raise ValueError("局部验证边距不能为负数。")
        preferred = next(
            (
                alternative
                for alternative in self.spec.alternatives
                if alternative.alternative_id == cached.alternative_id
            ),
            None,
        )
        empty = {
            anchor_id: self._missing_detection(anchor_id)
            for anchor_id in self.spec.anchors
        }
        if not cached.accepted or preferred is None:
            return RelativeLocatorResult(
                None,
                None,
                empty,
                ("cached_layout_invalid",),
                failure_code="cached_layout_invalid",
            )

        optional_anchors = {
            adjustment.anchor
            for alternative in self.spec.alternatives
            for adjustment in alternative.optional_adjustments
        }
        wanted = set(preferred.anchors)
        if not skip_optional_anchors:
            wanted.update(optional_anchors)

        scale_attempts = self._scale_attempts()
        for attempt_index, scales in enumerate(scale_attempts):
            candidate_map: dict[str, tuple[AnchorDetection, ...]] = {
                anchor_id: () for anchor_id in self.spec.anchors
            }
            for anchor_id in wanted:
                previous = cached.detections.get(anchor_id)
                if previous is None or not previous.accepted or previous.bounds is None:
                    # Optional anchors which were previously absent need a full
                    # scan to prove that they have not appeared somewhere else.
                    return RelativeLocatorResult(
                        None,
                        None,
                        empty,
                        (preferred.alternative_id,),
                        anchor_candidates=candidate_map,
                        failure_code="cached_layout_invalid",
                    )
                anchor = self.spec.anchors[anchor_id]
                max_width = max(
                    self._matcher_for(item, scales).template.width
                    for item in anchor.templates
                )
                max_height = max(
                    self._matcher_for(item, scales).template.height
                    for item in anchor.templates
                )
                roi = (
                    max(0, previous.bounds.left - padding),
                    max(0, previous.bounds.top - padding),
                    min(
                        image.width,
                        max(previous.bounds.right, previous.bounds.left + max_width)
                        + padding,
                    ),
                    min(
                        image.height,
                        max(previous.bounds.bottom, previous.bounds.top + max_height)
                        + padding,
                    ),
                )
                candidate_map[anchor_id] = self._candidates_for_anchor(
                    image,
                    anchor,
                    roi,
                    scales,
                )
            result = self._result_from_candidates(
                image,
                candidate_map,
                alternatives=(preferred,),
            )
            if (
                result.accepted
                or result.failure_code == "ambiguous_combinations"
                or attempt_index == len(scale_attempts) - 1
            ):
                return result
        raise AssertionError("scale attempts cannot be empty")

    def locate(
        self,
        image: Image.Image,
        *,
        skip_optional_anchors: bool = False,
    ) -> RelativeLocatorResult:
        optional_anchors = {
            adjustment.anchor
            for alternative in self.spec.alternatives
            for adjustment in alternative.optional_adjustments
        }
        scale_attempts = self._scale_attempts()
        for attempt_index, scales in enumerate(scale_attempts):
            candidates = self.detect_anchor_candidates(
                image,
                skip_anchors=optional_anchors if skip_optional_anchors else None,
                scale_factors=scales,
            )
            result = self._result_from_candidates(image, candidates)
            if (
                result.accepted
                or result.failure_code == "ambiguous_combinations"
                or attempt_index == len(scale_attempts) - 1
            ):
                return result
        raise AssertionError("scale attempts cannot be empty")

    def locate_with_diagnostics(
        self,
        image: Image.Image,
        *,
        skip_optional_anchors: bool = False,
    ) -> RelativeLocatorResult:
        """Locate normally, then attach near misses only when review is needed."""

        result = self.locate(image, skip_optional_anchors=skip_optional_anchors)
        if result.accepted:
            return replace(
                result,
                diagnostic_candidates=result.anchor_candidates or {},
            )
        optional_anchors = {
            adjustment.anchor
            for alternative in self.spec.alternatives
            for adjustment in alternative.optional_adjustments
        }
        scales = self._fallback_scale_factors or self._preferred_scale_factors
        diagnostic = self.detect_diagnostic_candidates(
            image,
            skip_anchors=optional_anchors if skip_optional_anchors else None,
            scale_factors=scales,
        )
        return replace(result, diagnostic_candidates=diagnostic)


def draw_debug_overlay(image: Image.Image, result: RelativeLocatorResult) -> Image.Image:
    """Draw every image candidate and every geometry-qualified target group."""

    output = image.convert("RGB").copy()
    draw = ImageDraw.Draw(output)
    colors = {
        "element1": "#d34a4a",
        "element2": "#3778c2",
        "element3": "#2a9d62",
        "element4_voice": "#8a55b5",
        "send_button": "#1b9e77",
        "emoji_button": "#3778c2",
    }
    candidate_map = result.anchor_candidates or {
        anchor_id: ((detection,) if detection.accepted else ())
        for anchor_id, detection in result.detections.items()
    }
    for anchor_id, candidates in candidate_map.items():
        color = colors.get(anchor_id, "#7a4ca3")
        for index, detection in enumerate(candidates, start=1):
            if detection.bounds is None:
                continue
            bounds = detection.bounds
            draw.rectangle(
                (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
                outline=color,
                width=1,
            )
            draw.text(
                (bounds.left + 2, max(0, bounds.top - 12)),
                f"{anchor_id}#{index} {detection.score:.2f} @{detection.scale:.2f}x",
                fill=color,
            )
    combination_colors = (
        "#ef8c28",
        "#d84b76",
        "#7c5bd6",
        "#1d9bb8",
        "#bf6b20",
        "#3b7d3f",
    )
    for index, combination in enumerate(result.distinct_combinations, start=1):
        target = combination.target
        color = combination_colors[(index - 1) % len(combination_colors)]
        draw.rectangle(
            (target.left, target.top, target.right - 1, target.bottom - 1),
            outline=color,
            width=2,
        )
        draw.text(
            (target.left + 2, max(0, target.top - 24)),
            f"COMBO {index}/{combination.alternative_id}",
            fill=color,
        )
    if result.target is not None:
        target = result.target
        draw.rectangle((target.left, target.top, target.right - 1, target.bottom - 1), outline="#ef8c28", width=3)
        draw.text((target.left + 2, max(0, target.top - 24)), f"TARGET/{result.alternative_id}", fill="#ef8c28")
        center = target.center
        draw.ellipse((center.x - 2, center.y - 2, center.x + 2, center.y + 2), fill="#ef8c28")
    if result.reference_bounds is not None and result.reference_bounds != result.target:
        reference = result.reference_bounds
        draw.rectangle(
            (reference.left, reference.top, reference.right - 1, reference.bottom - 1),
            outline="#2f9ca6",
            width=1,
        )
        draw.text((reference.left + 2, reference.bottom + 1), "ORIGINAL", fill="#257d85")
    if result.click_bounds is not None:
        safe = result.click_bounds
        draw.rectangle(
            (safe.left, safe.top, safe.right - 1, safe.bottom - 1),
            outline="#d0a000",
            width=1,
        )
        draw.text((safe.left + 2, safe.bottom + 1), "CLICK SAFE", fill="#a47d00")
    return output


def draw_combination_overlay(
    image: Image.Image,
    combination: RelativeLocatorCombination,
) -> Image.Image:
    """Draw one qualified combination alone for human ambiguity review."""

    output = image.convert("RGB").copy()
    draw = ImageDraw.Draw(output)
    colors = ("#1b9e77", "#3778c2", "#8a55b5", "#d34a4a")
    accepted = [
        combination.detections[anchor_id]
        for anchor_id in combination.used_anchor_ids
        if combination.detections[anchor_id].accepted
        and combination.detections[anchor_id].bounds is not None
    ]
    for index, detection in enumerate(accepted):
        bounds = detection.bounds
        assert bounds is not None
        color = colors[index % len(colors)]
        draw.rectangle(
            (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
            outline=color,
            width=2,
        )
        draw.text(
            (bounds.left + 2, max(0, bounds.top - 12)),
            f"{detection.anchor_id} {detection.score:.3f} @{detection.scale:.2f}x",
            fill=color,
        )
    target = combination.target
    draw.rectangle(
        (target.left, target.top, target.right - 1, target.bottom - 1),
        outline="#ef8c28",
        width=3,
    )
    draw.text(
        (target.left + 2, max(0, target.top - 24)),
        f"{combination.combination_id}/{combination.alternative_id}",
        fill="#ef8c28",
    )
    safe = combination.click_bounds
    draw.rectangle(
        (safe.left, safe.top, safe.right - 1, safe.bottom - 1),
        outline="#d0a000",
        width=1,
    )
    return output
