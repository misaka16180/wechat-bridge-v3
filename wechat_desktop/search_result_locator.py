"""Locate the first safe WeChat search result from visible section headers.

The search box supplies the horizontal frame of reference.  Stable section
headers (for example ``联系人`` and ``群聊``) supply the vertical reference.
Only the first row below the visually topmost qualified header is returned.
There is deliberately no fixed-Y, keyboard or blind-click fallback here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .models import Rect
from .relative_locator import (
    AnchorDetection,
    RelativeLocator,
    RelativeLocatorResult,
    draw_debug_overlay,
    load_relative_locator,
)
from .template_matching import OpenCVTemplateMatcher


class SearchResultLocatorConfigError(ValueError):
    pass


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchResultLocatorConfigError(f"{label} 必须是数字")
    return float(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchResultLocatorConfigError(f"{label} 必须是非空字符串")
    return value.strip()


def _fraction_range(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise SearchResultLocatorConfigError(f"{label} 必须是 [最小比例, 最大比例]")
    result = (_number(value[0], f"{label}[0]"), _number(value[1], f"{label}[1]"))
    if not 0 <= result[0] <= result[1] <= 1:
        raise SearchResultLocatorConfigError(f"{label} 必须位于 0 到 1 之间并按升序排列")
    return result


@dataclass(frozen=True)
class SectionTemplateSpec:
    path: Path
    minimum_score: float
    native_scale: float
    scales: tuple[float, ...]


@dataclass(frozen=True)
class SectionSpec:
    section_id: str
    label: str
    templates: tuple[SectionTemplateSpec, ...]


@dataclass(frozen=True)
class SearchResultLocatorSpec:
    source: Path
    locator_id: str
    base_locator: Path
    sections: tuple[SectionSpec, ...]
    scan_left_offset: float
    scan_right_offset: float
    scan_top_offset: float
    scan_maximum_depth: float
    diagnostic_score_floor: float
    maximum_candidates_per_template: int
    header_minimum_left_offset: float
    header_maximum_left_offset: float
    result_left_offset: float
    result_right_offset: float
    result_top_offset: float
    result_bottom_offset: float
    click_x_range: tuple[float, float]
    click_y_range: tuple[float, float]
    minimum_width: float
    maximum_width: float
    minimum_height: float
    maximum_height: float


@dataclass(frozen=True)
class SearchSectionDetection:
    section_id: str
    label: str
    template: Path
    bounds: Rect
    score: float
    scale: float
    score_accepted: bool
    geometry_accepted: bool

    @property
    def accepted(self) -> bool:
        return self.score_accepted and self.geometry_accepted


@dataclass(frozen=True)
class SearchResultLocatorResult:
    target: Rect | None
    click_bounds: Rect | None
    base: RelativeLocatorResult
    scan_bounds: Rect | None = None
    selected_header: SearchSectionDetection | None = None
    candidates: tuple[SearchSectionDetection, ...] = ()
    rejection_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return (
            self.target is not None
            and self.click_bounds is not None
            and self.selected_header is not None
        )

    def snapshot_result(self) -> RelativeLocatorResult:
        """Expose section candidates through the existing snapshot pipeline."""

        accepted_map: dict[str, tuple[AnchorDetection, ...]] = {}
        diagnostic_map: dict[str, tuple[AnchorDetection, ...]] = {}
        detections = dict(self.base.detections)
        by_section: dict[str, list[AnchorDetection]] = {}
        for candidate in self.candidates:
            anchor_id = f"search_section_{candidate.section_id}"
            by_section.setdefault(anchor_id, []).append(
                AnchorDetection(
                    anchor_id=anchor_id,
                    template=candidate.template,
                    bounds=candidate.bounds,
                    score=candidate.score,
                    second_score=0.0,
                    accepted=candidate.accepted,
                    scale=candidate.scale,
                )
            )
        for anchor_id, values in by_section.items():
            ordered = tuple(sorted(values, key=lambda item: item.score, reverse=True))
            diagnostic_map[anchor_id] = ordered
            accepted_map[anchor_id] = tuple(item for item in ordered if item.accepted)
            detections[anchor_id] = ordered[0]
        base_candidates = self.base.anchor_candidates or {}
        base_diagnostics = self.base.diagnostic_candidates or base_candidates
        return RelativeLocatorResult(
            alternative_id=(
                f"search_section_{self.selected_header.section_id}"
                if self.accepted and self.selected_header is not None
                else None
            ),
            target=self.target,
            detections=detections,
            rejected_alternatives=self.base.rejected_alternatives,
            click_bounds=self.click_bounds,
            reference_bounds=self.base.reference_bounds,
            anchor_candidates={**base_candidates, **accepted_map},
            valid_combinations=(),
            distinct_combinations=(),
            failure_code=self.rejection_code,
            diagnostic_candidates={**base_diagnostics, **diagnostic_map},
        )


def load_search_result_locator(path: str | Path) -> SearchResultLocatorSpec:
    source = Path(path).resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchResultLocatorConfigError(f"搜索结果定位 JSON 不存在：{source}") from exc
    except json.JSONDecodeError as exc:
        raise SearchResultLocatorConfigError(
            f"搜索结果定位 JSON 无效：{source}（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        raise SearchResultLocatorConfigError("搜索结果定位 JSON 顶层或版本无效")
    base_locator = (source.parent / _string(data.get("base_locator"), "base_locator")).resolve()
    if not base_locator.is_file():
        raise SearchResultLocatorConfigError(f"搜索框基础定位 JSON 不存在：{base_locator}")
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise SearchResultLocatorConfigError("sections 必须是非空数组")
    sections: list[SectionSpec] = []
    used_ids: set[str] = set()
    for section_index, raw_section in enumerate(raw_sections):
        label = f"sections[{section_index}]"
        if not isinstance(raw_section, dict):
            raise SearchResultLocatorConfigError(f"{label} 必须是对象")
        section_id = _string(raw_section.get("id"), f"{label}.id")
        if section_id in used_ids:
            raise SearchResultLocatorConfigError(f"分组编号重复：{section_id}")
        used_ids.add(section_id)
        raw_templates = raw_section.get("templates")
        if not isinstance(raw_templates, list) or not raw_templates:
            raise SearchResultLocatorConfigError(f"{label}.templates 必须是非空数组")
        templates: list[SectionTemplateSpec] = []
        for template_index, raw_template in enumerate(raw_templates):
            template_label = f"{label}.templates[{template_index}]"
            if not isinstance(raw_template, dict):
                raise SearchResultLocatorConfigError(f"{template_label} 必须是对象")
            template_path = (
                source.parent / _string(raw_template.get("path"), f"{template_label}.path")
            ).resolve()
            if not template_path.is_file():
                raise SearchResultLocatorConfigError(f"分组标题模板不存在：{template_path}")
            minimum_score = _number(
                raw_template.get("minimum_score", 0.90),
                f"{template_label}.minimum_score",
            )
            native_scale = _number(
                raw_template.get("native_scale", 1.0),
                f"{template_label}.native_scale",
            )
            raw_scales = raw_template.get("scales", [1.0])
            if not isinstance(raw_scales, list) or not raw_scales:
                raise SearchResultLocatorConfigError(f"{template_label}.scales 必须是非空数组")
            scales = tuple(_number(value, f"{template_label}.scales") for value in raw_scales)
            if not 0 <= minimum_score <= 1 or not 0.5 <= native_scale <= 3:
                raise SearchResultLocatorConfigError(f"{template_label} 的阈值或原生 DPI 无效")
            if any(not 0.5 <= value <= 3 for value in scales):
                raise SearchResultLocatorConfigError(f"{template_label}.scales 必须位于 0.5 到 3.0")
            templates.append(
                SectionTemplateSpec(template_path, minimum_score, native_scale, scales)
            )
        sections.append(
            SectionSpec(
                section_id,
                _string(raw_section.get("label"), f"{label}.label"),
                tuple(templates),
            )
        )
    scan = data.get("scan") or {}
    geometry = data.get("header_geometry") or {}
    result = data.get("result") or {}
    click = data.get("click") or {}
    bounds = data.get("bounds") or {}
    for value, label in (
        (scan, "scan"),
        (geometry, "header_geometry"),
        (result, "result"),
        (click, "click"),
        (bounds, "bounds"),
    ):
        if not isinstance(value, dict):
            raise SearchResultLocatorConfigError(f"{label} 必须是对象")
    maximum_candidates = int(
        _number(scan.get("maximum_candidates_per_template", 12), "scan.maximum_candidates_per_template")
    )
    if not 1 <= maximum_candidates <= 32:
        raise SearchResultLocatorConfigError("每个分组模板候选上限必须在 1 到 32 之间")
    diagnostic_floor = _number(
        scan.get("diagnostic_score_floor", 0.70), "scan.diagnostic_score_floor"
    )
    if not 0 <= diagnostic_floor <= 1:
        raise SearchResultLocatorConfigError("诊断候选分数下限必须位于 0 到 1 之间")
    spec = SearchResultLocatorSpec(
        source=source,
        locator_id=_string(data.get("id", source.stem), "id"),
        base_locator=base_locator,
        sections=tuple(sections),
        scan_left_offset=_number(scan.get("left_offset", 0), "scan.left_offset"),
        scan_right_offset=_number(scan.get("right_offset", 0), "scan.right_offset"),
        scan_top_offset=_number(scan.get("top_offset", 0), "scan.top_offset"),
        scan_maximum_depth=_number(scan.get("maximum_depth", 500), "scan.maximum_depth"),
        diagnostic_score_floor=diagnostic_floor,
        maximum_candidates_per_template=maximum_candidates,
        header_minimum_left_offset=_number(
            geometry.get("minimum_left_offset", -2), "header_geometry.minimum_left_offset"
        ),
        header_maximum_left_offset=_number(
            geometry.get("maximum_left_offset", 18), "header_geometry.maximum_left_offset"
        ),
        result_left_offset=_number(result.get("left_offset", 52), "result.left_offset"),
        result_right_offset=_number(result.get("right_offset", 0), "result.right_offset"),
        result_top_offset=_number(
            result.get("top_from_header_bottom", 4), "result.top_from_header_bottom"
        ),
        result_bottom_offset=_number(
            result.get("bottom_from_header_bottom", 68), "result.bottom_from_header_bottom"
        ),
        click_x_range=_fraction_range(click.get("x_fraction", [0.15, 0.85]), "click.x_fraction"),
        click_y_range=_fraction_range(click.get("y_fraction", [0.20, 0.80]), "click.y_fraction"),
        minimum_width=_number(bounds.get("minimum_width", 40), "bounds.minimum_width"),
        maximum_width=_number(bounds.get("maximum_width", 500), "bounds.maximum_width"),
        minimum_height=_number(bounds.get("minimum_height", 50), "bounds.minimum_height"),
        maximum_height=_number(bounds.get("maximum_height", 80), "bounds.maximum_height"),
    )
    if spec.header_minimum_left_offset > spec.header_maximum_left_offset:
        raise SearchResultLocatorConfigError("标题左边缘偏移范围无效")
    if not 0 < spec.result_top_offset < spec.result_bottom_offset:
        raise SearchResultLocatorConfigError("第一行相对标题的纵向偏移无效")
    if not (
        0 < spec.minimum_width <= spec.maximum_width
        and 0 < spec.minimum_height <= spec.maximum_height
    ):
        raise SearchResultLocatorConfigError("第一行尺寸范围无效")
    return spec


def _click_bounds(
    target: Rect,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> Rect:
    left = target.left + int(target.width * x_range[0])
    right = target.left + max(int(target.width * x_range[1]), int(target.width * x_range[0]) + 1)
    top = target.top + int(target.height * y_range[0])
    bottom = target.top + max(int(target.height * y_range[1]), int(target.height * y_range[0]) + 1)
    return Rect(left, top, min(target.right, right), min(target.bottom, bottom))


class SearchResultSectionLocator:
    def __init__(self, spec: SearchResultLocatorSpec):
        self.spec = spec
        self.base_locator = RelativeLocator(load_relative_locator(spec.base_locator))
        self._preferred_scales: tuple[float, ...] | None = None
        self._fallback_scales: tuple[float, ...] | None = None
        self._matchers: dict[tuple[str, float, tuple[float, ...]], OpenCVTemplateMatcher] = {}

    @staticmethod
    def _normalise_scales(values: tuple[float, ...] | list[float]) -> tuple[float, ...]:
        result: list[float] = []
        for raw in values:
            scale = round(float(raw), 4)
            if not 0.5 <= scale <= 3.0:
                raise ValueError("DPI 缩放比例必须位于 0.5 到 3.0 之间。")
            if scale not in result:
                result.append(scale)
        if not result:
            raise ValueError("DPI 缩放比例不能为空。")
        return tuple(result)

    def set_scale_policy(
        self,
        preferred: tuple[float, ...] | list[float],
        fallback: tuple[float, ...] | list[float] | None = None,
    ) -> None:
        self._preferred_scales = self._normalise_scales(preferred)
        self._fallback_scales = self._normalise_scales(fallback or preferred)
        self.base_locator.set_scale_policy(self._preferred_scales, self._fallback_scales)

    def _scale_attempts(self) -> tuple[float | None, ...]:
        if self._preferred_scales is None:
            return (None,)
        ordered = list(self._preferred_scales)
        for scale in self._fallback_scales or ():
            if scale not in ordered:
                ordered.append(scale)
        return tuple(ordered)

    @staticmethod
    def _template_tiers(
        section: SectionSpec,
        display_scale: float | None,
    ) -> tuple[tuple[SectionTemplateSpec, ...], ...]:
        if display_scale is None:
            return (section.templates,)
        native = tuple(
            item
            for item in section.templates
            if math.isclose(item.native_scale, display_scale, abs_tol=0.0001)
        )
        other = tuple(item for item in section.templates if item not in native)
        if native and other:
            return native, other
        return (native or other,)

    def _matcher(
        self,
        template: SectionTemplateSpec,
        display_scale: float | None,
    ) -> OpenCVTemplateMatcher:
        scales = (
            template.scales
            if display_scale is None
            else (round(display_scale / template.native_scale, 6),)
        )
        key = (str(template.path), template.minimum_score, scales)
        matcher = self._matchers.get(key)
        if matcher is None:
            matcher = OpenCVTemplateMatcher(
                template.path,
                minimum_score=template.minimum_score,
                minimum_margin=0.0,
                scale_factors=scales,
            )
            self._matchers[key] = matcher
        return matcher

    @staticmethod
    def _same_hit(left: SearchSectionDetection, right: SearchSectionDetection) -> bool:
        return (
            left.section_id == right.section_id
            and abs(left.bounds.center.x - right.bounds.center.x)
            <= max(3, min(left.bounds.width, right.bounds.width) // 3)
            and abs(left.bounds.center.y - right.bounds.center.y)
            <= max(3, min(left.bounds.height, right.bounds.height) // 3)
        )

    def _scan_bounds(self, image: Image.Image, base_bounds: Rect, scale: float) -> Rect | None:
        left = max(0, round(base_bounds.left + self.spec.scan_left_offset * scale))
        right = min(image.width, round(base_bounds.right + self.spec.scan_right_offset * scale))
        top = max(0, round(base_bounds.bottom + self.spec.scan_top_offset * scale))
        bottom = min(image.height, round(base_bounds.bottom + self.spec.scan_maximum_depth * scale))
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right, bottom)

    def _scan_attempt(
        self,
        image: Image.Image,
        base_bounds: Rect,
        display_scale: float | None,
    ) -> tuple[Rect | None, tuple[SearchSectionDetection, ...]]:
        effective_scale = display_scale or 1.0
        scan_bounds = self._scan_bounds(image, base_bounds, effective_scale)
        if scan_bounds is None:
            return None, ()
        roi = (scan_bounds.left, scan_bounds.top, scan_bounds.right, scan_bounds.bottom)
        found: list[SearchSectionDetection] = []
        for section in self.spec.sections:
            section_values: list[SearchSectionDetection] = []
            for tier in self._template_tiers(section, display_scale):
                tier_values: list[SearchSectionDetection] = []
                for template in tier:
                    matcher = self._matcher(template, display_scale)
                    candidates = matcher.find_diagnostic_candidates(
                        image,
                        roi=roi,
                        max_candidates=self.spec.maximum_candidates_per_template,
                        score_floor=self.spec.diagnostic_score_floor,
                    )
                    for candidate in candidates:
                        total_scale = round(candidate.scale * template.native_scale, 4)
                        left_offset = candidate.bounds.left - base_bounds.left
                        geometry_accepted = (
                            self.spec.header_minimum_left_offset * total_scale
                            <= left_offset
                            <= self.spec.header_maximum_left_offset * total_scale
                        )
                        tier_values.append(
                            SearchSectionDetection(
                                section.section_id,
                                section.label,
                                template.path,
                                candidate.bounds,
                                candidate.score,
                                total_scale,
                                candidate.score >= template.minimum_score,
                                geometry_accepted,
                            )
                        )
                section_values.extend(tier_values)
                if any(item.accepted for item in tier_values):
                    break
            for candidate in sorted(section_values, key=lambda item: item.score, reverse=True):
                duplicate = next(
                    (index for index, existing in enumerate(found) if self._same_hit(existing, candidate)),
                    None,
                )
                if duplicate is None:
                    found.append(candidate)
                elif candidate.score > found[duplicate].score:
                    found[duplicate] = candidate
        found.sort(
            key=lambda item: (
                item.bounds.top,
                item.bounds.left,
                -item.score,
                next(
                    index
                    for index, section in enumerate(self.spec.sections)
                    if section.section_id == item.section_id
                ),
            )
        )
        return scan_bounds, tuple(found)

    def locate(self, image: Image.Image) -> SearchResultLocatorResult:
        base = self.base_locator.locate(image, skip_optional_anchors=True)
        return self.locate_from_base(image, base)

    def locate_from_base(
        self,
        image: Image.Image,
        base: RelativeLocatorResult,
    ) -> SearchResultLocatorResult:
        if not base.accepted:
            return SearchResultLocatorResult(
                None,
                None,
                base,
                rejection_code="base_locator_rejected",
                details={"base_failure_code": base.failure_code},
            )
        base_bounds = base.reference_bounds
        if base_bounds is None:
            return SearchResultLocatorResult(
                None,
                None,
                base,
                rejection_code="search_reference_missing",
            )
        all_candidates: list[SearchSectionDetection] = []
        last_scan: Rect | None = None
        selected: SearchSectionDetection | None = None
        selected_scale = 1.0
        for display_scale in self._scale_attempts():
            last_scan, attempt = self._scan_attempt(image, base_bounds, display_scale)
            all_candidates.extend(attempt)
            accepted = [item for item in attempt if item.accepted]
            if accepted:
                accepted.sort(
                    key=lambda item: (
                        item.bounds.top,
                        item.bounds.left,
                        -item.score,
                        next(
                            index
                            for index, section in enumerate(self.spec.sections)
                            if section.section_id == item.section_id
                        ),
                    )
                )
                selected = accepted[0]
                selected_scale = selected.scale
                break
        if selected is None:
            return SearchResultLocatorResult(
                None,
                None,
                base,
                scan_bounds=last_scan,
                candidates=tuple(all_candidates),
                rejection_code="section_header_missing",
                details={
                    "qualified_header_count": 0,
                    "diagnostic_candidate_count": len(all_candidates),
                    "allowed_sections": [section.section_id for section in self.spec.sections],
                },
            )
        edges = {
            "left": round(base_bounds.left + self.spec.result_left_offset * selected_scale),
            "right": round(base_bounds.right + self.spec.result_right_offset * selected_scale),
            "top": round(selected.bounds.bottom + self.spec.result_top_offset * selected_scale),
            "bottom": round(selected.bounds.bottom + self.spec.result_bottom_offset * selected_scale),
        }
        try:
            target = Rect(edges["left"], edges["top"], edges["right"], edges["bottom"])
        except ValueError:
            return SearchResultLocatorResult(
                None,
                None,
                base,
                scan_bounds=last_scan,
                selected_header=selected,
                candidates=tuple(all_candidates),
                rejection_code="target_rectangle_invalid",
                details={"calculated_edges": edges},
            )
        minimum_width = self.spec.minimum_width * selected_scale
        maximum_width = self.spec.maximum_width * selected_scale
        minimum_height = self.spec.minimum_height * selected_scale
        maximum_height = self.spec.maximum_height * selected_scale
        if not (
            minimum_width <= target.width <= maximum_width
            and minimum_height <= target.height <= maximum_height
        ):
            return SearchResultLocatorResult(
                None,
                None,
                base,
                scan_bounds=last_scan,
                selected_header=selected,
                candidates=tuple(all_candidates),
                rejection_code="target_size_out_of_bounds",
                details={"target": edges, "scale": selected_scale},
            )
        if not (
            0 <= target.left < target.right <= image.width
            and 0 <= target.top < target.bottom <= image.height
        ):
            return SearchResultLocatorResult(
                None,
                None,
                base,
                scan_bounds=last_scan,
                selected_header=selected,
                candidates=tuple(all_candidates),
                rejection_code="target_out_of_image",
                details={"target": edges, "image_size": image.size},
            )
        return SearchResultLocatorResult(
            target,
            _click_bounds(target, self.spec.click_x_range, self.spec.click_y_range),
            base,
            scan_bounds=last_scan,
            selected_header=selected,
            candidates=tuple(all_candidates),
            details={
                "selected_section": selected.section_id,
                "selected_label": selected.label,
                "selected_score": round(selected.score, 6),
                "selected_scale": selected.scale,
                "qualified_header_count": sum(item.accepted for item in all_candidates),
            },
        )


def draw_search_result_debug_overlay(
    image: Image.Image,
    result: SearchResultLocatorResult,
) -> Image.Image:
    output = draw_debug_overlay(image, result.base)
    draw = ImageDraw.Draw(output)
    if result.scan_bounds is not None:
        bounds = result.scan_bounds
        draw.rectangle(
            (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
            outline="#6d7f92",
            width=1,
        )
        draw.text((bounds.left + 2, bounds.top + 2), "SECTION SCAN", fill="#4d6277")
    for candidate in result.candidates:
        bounds = candidate.bounds
        color = "#2a9d55" if candidate.accepted else "#d38324"
        width = 2 if candidate.accepted else 1
        draw.rectangle(
            (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
            outline=color,
            width=width,
        )
        draw.text(
            (bounds.left + 1, max(0, bounds.top - 12)),
            f"{candidate.section_id} {candidate.score:.3f}",
            fill=color,
        )
    if result.selected_header is not None:
        bounds = result.selected_header.bounds
        draw.rectangle(
            (bounds.left - 2, bounds.top - 2, bounds.right + 1, bounds.bottom + 1),
            outline="#1976d2",
            width=3,
        )
        draw.text((bounds.right + 4, bounds.top), "TOP SECTION", fill="#1976d2")
    if result.target is not None:
        bounds = result.target
        draw.rectangle(
            (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
            outline="#b24aa5",
            width=2,
        )
        draw.text((bounds.left + 2, max(0, bounds.top - 12)), "FIRST ROW", fill="#9a338d")
    if result.click_bounds is not None:
        bounds = result.click_bounds
        draw.rectangle(
            (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
            outline="#d0a000",
            width=2,
        )
        draw.text((bounds.left + 2, bounds.bottom + 2), "CLICK SAFE", fill="#a47d00")
    return output


__all__ = [
    "SearchResultLocatorConfigError",
    "SearchResultLocatorResult",
    "SearchResultLocatorSpec",
    "SearchResultSectionLocator",
    "SearchSectionDetection",
    "draw_search_result_debug_overlay",
    "load_search_result_locator",
]
