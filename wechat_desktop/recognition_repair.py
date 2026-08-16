"""Reviewable, candidate-based repair of local visual recognition profiles.

The repair flow deliberately accepts candidate IDs from a saved recognition
snapshot, never arbitrary browser coordinates.  A single selected image is
only a proposal; the complete anchor group is revalidated before any local
profile is written.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image

from .models import Rect
from .recognition_snapshot import RecognitionSnapshotStore
from .relative_locator import (
    MAX_REVIEW_CANDIDATES_PER_ANCHOR,
    AnchorTemplateSpec,
    AlternativeSpec,
    RelativeLocator,
    RelativeLocatorResult,
    ScalarReferenceSpec,
    adapt_constraint_to_observation,
    local_constraint_adaptation_policy,
    load_relative_locator,
)


class RecognitionRepairError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


@dataclass(frozen=True)
class RepairAlternative:
    alternative_id: str
    label: str
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class RepairTarget:
    target_id: str
    label: str
    locator_file: str
    anchor_labels: dict[str, str]
    alternatives: tuple[RepairAlternative, ...]


@dataclass(frozen=True)
class PreparedRepair:
    snapshot: dict[str, Any]
    target: RepairTarget
    alternative: RepairAlternative
    locator_alternative: AlternativeSpec
    result: RelativeLocatorResult
    selected: dict[str, Path]
    percent: int
    scale: float
    geometry_checks: tuple[dict[str, Any], ...]


REPAIR_TARGETS = (
    RepairTarget(
        "search_box",
        "微信搜索框",
        "search_box_anchors.json",
        {
            "element1": "左上工具栏的加号",
            "element2": "左侧栏的联系人图标",
            "element3": "左侧栏的小程序方块图标",
        },
        (
            RepairAlternative("element1_plus_element2", "加号 + 联系人图标", ("element1", "element2")),
            RepairAlternative("element1_plus_element3", "加号 + 小程序方块图标", ("element1", "element3")),
        ),
    ),
    RepairTarget(
        "chat_input",
        "消息输入区域",
        "chat_input_by_toolbar.json",
        {
            "send_button": "发送按钮",
            "emoji_button": "输入区表情按钮",
        },
        (
            RepairAlternative(
                "send_button_plus_emoji_button",
                "发送按钮 + 表情按钮",
                ("send_button", "emoji_button"),
            ),
        ),
    ),
)


class RecognitionRepairManager:
    def __init__(
        self,
        snapshot_store: RecognitionSnapshotStore,
        *,
        locator_root: str | Path | None = None,
        profile_root: str | Path | None = None,
        targets: tuple[RepairTarget, ...] = REPAIR_TARGETS,
    ) -> None:
        package_root = Path(__file__).resolve().parents[1]
        self.snapshot_store = snapshot_store
        self.locator_root = Path(locator_root or package_root / "locators").resolve()
        # Keep the on-disk directory stable so existing local profiles survive
        # the UI/API rename from calibration to recognition repair.
        self.root = Path(profile_root or package_root / "local_calibration").resolve()
        protected_roots = (self.locator_root, (package_root / "templates").resolve())
        for protected in protected_roots:
            try:
                self.root.relative_to(protected)
                overlaps = True
            except ValueError:
                try:
                    protected.relative_to(self.root)
                    overlaps = True
                except ValueError:
                    overlaps = False
            if overlaps:
                raise RecognitionRepairError(
                    "repair_profile_root_unsafe",
                    "本机识别图片目录不能与程序内置定位规则或模板目录重叠。",
                )
        self.targets = {item.target_id: item for item in targets}

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def _manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if value.get("version") == 1 and isinstance(value.get("profiles"), dict):
                return value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {
            "version": 1,
            "kind": "machine_local_template_overlay",
            "profiles": {},
        }

    def _write_manifest(self, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def status(self) -> dict[str, Any]:
        profiles = dict(self._manifest().get("profiles") or {})
        result: list[dict[str, Any]] = []
        for target in self.targets.values():
            locator = load_relative_locator(
                self.locator_root / target.locator_file,
                include_local=False,
            )
            profile = dict(profiles.get(locator.locator_id) or {})
            result.append(
                {
                    "id": target.target_id,
                    "label": target.label,
                    "locator_id": locator.locator_id,
                    "anchor_labels": dict(target.anchor_labels),
                    "alternatives": [
                        {
                            "id": item.alternative_id,
                            "label": item.label,
                            "anchors": [
                                {"id": anchor, "label": target.anchor_labels.get(anchor, anchor)}
                                for anchor in item.anchors
                            ],
                        }
                        for item in target.alternatives
                    ],
                    "profile": {
                        "active": bool(profile.get("active")),
                        "updated_at": profile.get("updated_at"),
                        "source_snapshot_id": str(profile.get("source_snapshot_id") or ""),
                        "scale_percent": profile.get("scale_percent"),
                        "alternative_id": str(profile.get("alternative_id") or ""),
                    },
                }
            )
        return {"ok": True, "targets": result}

    def _target(self, target_id: str, alternative_id: str) -> tuple[RepairTarget, RepairAlternative]:
        target = self.targets.get(str(target_id or ""))
        if target is None:
            raise RecognitionRepairError("repair_target_invalid", "无法判断需要修复的微信位置。")
        alternative = next(
            (item for item in target.alternatives if item.alternative_id == str(alternative_id or "")),
            None,
        )
        if alternative is None:
            raise RecognitionRepairError("repair_alternative_invalid", "请选择一组完整的参照元素。")
        return target, alternative

    def _snapshot(self, snapshot_id: str) -> tuple[dict[str, Any], Path]:
        item = next(
            (
                value
                for value in self.snapshot_store.list(source="compatibility", limit=120)
                if str(value.get("id") or "") == str(snapshot_id or "")
            ),
            None,
        )
        if item is None:
            raise RecognitionRepairError(
                "repair_snapshot_not_found",
                "识别记录不存在或已过期，请重新运行界面检查。",
            )
        if item.get("outcome") == "success":
            raise RecognitionRepairError(
                "repair_snapshot_not_failed",
                "这条识别记录已经通过，不需要修复。",
            )
        try:
            original = self.snapshot_store.image_path(str(snapshot_id), "original")
        except KeyError as exc:
            raise RecognitionRepairError("repair_snapshot_not_found", str(exc)) from exc
        return item, original

    @staticmethod
    def _scale(item: dict[str, Any]) -> tuple[int, float]:
        environment = dict((item.get("extra") or {}).get("environment") or {})
        assessment = dict(environment.get("scale_assessment") or {})
        value = assessment.get("suggested_scale_percent") or environment.get("scale_percent") or 100
        try:
            percent = int(value)
        except (TypeError, ValueError):
            percent = 100
        percent = max(50, min(300, percent))
        return percent, round(percent / 100.0, 4)

    @staticmethod
    def _candidate_scales(item: dict[str, Any]) -> tuple[float, ...]:
        environment = dict((item.get("extra") or {}).get("environment") or {})
        assessment = dict(environment.get("scale_assessment") or {})
        values = assessment.get("attempted_scale_percents") or []
        scales: list[float] = []
        if isinstance(values, list):
            for value in values:
                try:
                    scale = round(float(value) / 100.0, 4)
                except (TypeError, ValueError):
                    continue
                if 0.5 <= scale <= 3.0 and scale not in scales:
                    scales.append(scale)
        if not scales:
            scales.append(RecognitionRepairManager._scale(item)[1])
        return tuple(scales)

    @staticmethod
    def _candidate_score_floor(value: Any) -> float:
        if isinstance(value, bool):
            raise RecognitionRepairError(
                "repair_candidate_threshold_invalid",
                "候选最低匹配分数必须是 0 到 1 之间的数字。",
            )
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise RecognitionRepairError(
                "repair_candidate_threshold_invalid",
                "候选最低匹配分数必须是 0 到 1 之间的数字。",
            ) from exc
        if not 0.0 <= score <= 1.0:
            raise RecognitionRepairError(
                "repair_candidate_threshold_invalid",
                "候选最低匹配分数必须位于 0 到 1 之间。",
            )
        return round(score, 4)

    def reload_candidates(
        self,
        *,
        snapshot_id: str,
        target_id: str,
        minimum_score: Any,
    ) -> dict[str, Any]:
        target = self.targets.get(str(target_id or ""))
        if target is None:
            raise RecognitionRepairError(
                "repair_target_invalid",
                "无法判断需要重新扫描的微信位置。",
            )
        score_floor = self._candidate_score_floor(minimum_score)
        snapshot, original_path = self._snapshot(snapshot_id)
        if str((snapshot.get("extra") or {}).get("check_id") or "") != target.target_id:
            raise RecognitionRepairError(
                "repair_snapshot_target_mismatch",
                "这条识别记录与当前修复位置不一致，请重新选择记录。",
            )

        spec = load_relative_locator(
            self.locator_root / target.locator_file,
            include_local=False,
        )
        wanted_anchors = {
            anchor
            for alternative in target.alternatives
            for anchor in alternative.anchors
        }
        skip_anchors = set(spec.anchors) - wanted_anchors
        scales = self._candidate_scales(snapshot)
        with Image.open(original_path) as opened:
            image = opened.convert("RGB")
        locator = RelativeLocator(spec)
        candidate_map = locator.detect_diagnostic_candidates(
            image,
            skip_anchors=skip_anchors,
            scale_factors=scales,
            max_candidates=MAX_REVIEW_CANDIDATES_PER_ANCHOR,
            score_floor=score_floor,
        )
        filtered = {
            anchor_id: values
            for anchor_id, values in candidate_map.items()
            if anchor_id in wanted_anchors
        }
        updated = self.snapshot_store.replace_review_candidates(
            str(snapshot_id),
            filtered,
            score_floor=score_floor,
            candidate_limit=MAX_REVIEW_CANDIDATES_PER_ANCHOR,
        )
        counts = dict(updated.get("review_candidate_counts") or {})
        reached = [
            target.anchor_labels.get(anchor_id, anchor_id)
            for anchor_id, value in dict(
                updated.get("review_candidate_limit_reached") or {}
            ).items()
            if value
        ]
        total = sum(int(value or 0) for value in counts.values())
        message = (
            f"已按最低匹配分数 {score_floor:.2f} 重新扫描，共加载 {total} 个候选。"
        )
        if reached:
            message += (
                "以下元素已达到每项 "
                f"{MAX_REVIEW_CANDIDATES_PER_ANCHOR} 个的安全上限："
                + "、".join(reached)
                + "。"
            )
        elif not total:
            message += "当前阈值下没有候选，可以适当降低后重试。"
        return {
            "ok": True,
            "message": message,
            "minimum_score": score_floor,
            "candidate_limit": MAX_REVIEW_CANDIDATES_PER_ANCHOR,
            "candidate_counts": counts,
            "limit_reached": reached,
            "snapshot": updated,
        }

    @staticmethod
    def _rect(value: Any, image: Image.Image, label: str) -> Rect:
        if not isinstance(value, dict):
            raise RecognitionRepairError("repair_candidate_invalid", f"未找到“{label}”的候选位置。")
        try:
            rect = Rect(
                int(value["left"]),
                int(value["top"]),
                int(value["right"]),
                int(value["bottom"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecognitionRepairError("repair_candidate_invalid", f"“{label}”的候选位置无效。") from exc
        if rect.left < 0 or rect.top < 0 or rect.right > image.width or rect.bottom > image.height:
            raise RecognitionRepairError("repair_candidate_invalid", f"“{label}”的候选位置超出截图范围。")
        if rect.width < 2 or rect.height < 2:
            raise RecognitionRepairError("repair_candidate_invalid", f"“{label}”的候选区域太小。")
        return rect

    @staticmethod
    def _candidate(
        snapshot: dict[str, Any],
        anchor: str,
        candidate_id: Any,
    ) -> dict[str, Any]:
        wanted = str(candidate_id or "")
        values = dict(snapshot.get("review_candidates") or {}).get(anchor) or []
        for value in values:
            if str(value.get("candidate_id") or "") == wanted:
                return dict(value)
        raise RecognitionRepairError(
            "repair_candidate_not_found",
            f"没有在这条识别记录中找到“{anchor}”的候选，请刷新后重新选择。",
        )

    @staticmethod
    def _reference_value(
        reference: ScalarReferenceSpec,
        selected_bounds: dict[str, Rect],
        image: Image.Image,
    ) -> float:
        if reference.source == "image":
            if reference.reference in {"left", "top"}:
                return 0.0
            if reference.reference == "right":
                return float(image.width)
            if reference.reference == "bottom":
                return float(image.height)
            if reference.reference == "center_x":
                return float(image.width // 2)
            if reference.reference == "center_y":
                return float(image.height // 2)
            raise AssertionError(reference.reference)
        bounds = selected_bounds[reference.source]
        if reference.reference == "left":
            return float(bounds.left)
        if reference.reference == "top":
            return float(bounds.top)
        if reference.reference == "right":
            return float(bounds.right)
        if reference.reference == "bottom":
            return float(bounds.bottom)
        if reference.reference == "center_x":
            return float(bounds.left + bounds.width // 2)
        if reference.reference == "center_y":
            return float(bounds.top + bounds.height // 2)
        raise AssertionError(reference.reference)

    @staticmethod
    def _display_number(value: float) -> str:
        if abs(value - round(value)) < 0.000001:
            return str(int(round(value)))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    @classmethod
    def _adaptive_geometry(
        cls,
        alternative: AlternativeSpec,
        selected_bounds: dict[str, Rect],
        image: Image.Image,
        scale: float,
    ) -> tuple[AlternativeSpec, tuple[dict[str, Any], ...]]:
        adjusted_constraints = []
        checks: list[dict[str, Any]] = []
        for index, constraint in enumerate(alternative.constraints):
            left = cls._reference_value(constraint.left, selected_bounds, image)
            right = cls._reference_value(constraint.right, selected_bounds, image)
            actual_pixels = left - right
            adjusted, status, actual_logical = adapt_constraint_to_observation(
                constraint,
                actual_pixels,
                scale,
            )
            expansion, _padding = local_constraint_adaptation_policy(constraint)
            original_min_pixels = constraint.min_difference * scale
            original_max_pixels = constraint.max_difference * scale
            safe_min_pixels = (constraint.min_difference - expansion) * scale
            safe_max_pixels = (constraint.max_difference + expansion) * scale
            adjusted_min_pixels = (
                adjusted.min_difference * scale if adjusted is not None else None
            )
            adjusted_max_pixels = (
                adjusted.max_difference * scale if adjusted is not None else None
            )
            description = constraint.description or f"位置关系检查 {index + 1}"
            actual_text = cls._display_number(actual_pixels)
            original_text = (
                cls._display_number(original_min_pixels)
                + "～"
                + cls._display_number(original_max_pixels)
            )
            if status == "passed":
                message = f"实际 {actual_text} px；符合程序默认范围 {original_text} px。"
            elif status == "adapted" and adjusted is not None:
                adjusted_text = (
                    cls._display_number(float(adjusted_min_pixels))
                    + "～"
                    + cls._display_number(float(adjusted_max_pixels))
                )
                message = (
                    f"实际 {actual_text} px；程序默认范围 {original_text} px；"
                    f"已为本机安全扩展至 {adjusted_text} px。"
                )
            else:
                safe_text = (
                    cls._display_number(safe_min_pixels)
                    + "～"
                    + cls._display_number(safe_max_pixels)
                )
                message = (
                    f"实际 {actual_text} px；程序默认范围 {original_text} px；"
                    f"超出允许的本机自适应上限 {safe_text} px。"
                )
            checks.append(
                {
                    "index": index,
                    "description": description,
                    "status": status,
                    "actual_pixels": round(actual_pixels, 4),
                    "actual_logical": round(actual_logical, 4),
                    "original_min": constraint.min_difference,
                    "original_max": constraint.max_difference,
                    "original_min_pixels": round(original_min_pixels, 4),
                    "original_max_pixels": round(original_max_pixels, 4),
                    "adjusted_min": (
                        adjusted.min_difference if adjusted is not None else None
                    ),
                    "adjusted_max": (
                        adjusted.max_difference if adjusted is not None else None
                    ),
                    "adjusted_min_pixels": (
                        round(adjusted_min_pixels, 4)
                        if adjusted_min_pixels is not None
                        else None
                    ),
                    "adjusted_max_pixels": (
                        round(adjusted_max_pixels, 4)
                        if adjusted_max_pixels is not None
                        else None
                    ),
                    "safe_min_pixels": round(safe_min_pixels, 4),
                    "safe_max_pixels": round(safe_max_pixels, 4),
                    "message": message,
                }
            )
            adjusted_constraints.append(adjusted or constraint)

        failed = [item for item in checks if item["status"] == "failed"]
        if failed:
            lines = [
                f"这组候选有 {len(failed)} 项位置关系超出安全范围，未保存任何识别图片。"
            ]
            lines.extend(
                f"{item['index'] + 1}. {item['description']}：{item['message']}"
                for item in checks
            )
            raise RecognitionRepairError(
                "repair_geometry_out_of_range",
                "\n".join(lines),
                details={"geometry_checks": checks},
            )
        return (
            replace(alternative, constraints=tuple(adjusted_constraints)),
            tuple(checks),
        )

    @staticmethod
    def _geometry_manifest(alternative: AlternativeSpec) -> dict[str, Any]:
        return {
            "version": 1,
            "alternative_id": alternative.alternative_id,
            "constraints": [
                {
                    "index": index,
                    "left": {
                        "source": constraint.left.source,
                        "reference": constraint.left.reference,
                    },
                    "right": {
                        "source": constraint.right.source,
                        "reference": constraint.right.reference,
                    },
                    "min_difference": constraint.min_difference,
                    "max_difference": constraint.max_difference,
                }
                for index, constraint in enumerate(alternative.constraints)
            ],
        }

    def _prepare_validation(
        self,
        *,
        snapshot_id: str,
        target_id: str,
        alternative_id: str,
        candidate_ids: Any,
        staging: Path,
    ) -> PreparedRepair:
        target, alternative = self._target(target_id, alternative_id)
        if not isinstance(candidate_ids, dict):
            raise RecognitionRepairError("repair_candidates_missing", "请为每个参照元素选择一个候选。")
        snapshot, original_path = self._snapshot(snapshot_id)
        spec = load_relative_locator(
            self.locator_root / target.locator_file,
            include_local=False,
        )
        spec_alternative = next(
            (item for item in spec.alternatives if item.alternative_id == alternative.alternative_id),
            None,
        )
        if spec_alternative is None or spec_alternative.anchors != alternative.anchors:
            raise RecognitionRepairError("repair_definition_changed", "定位规则已更新，请重新运行界面检查。")
        percent, scale = self._scale(snapshot)
        staging.mkdir(parents=True, exist_ok=True)
        with Image.open(original_path) as opened:
            image = opened.convert("RGB")

        anchors = dict(spec.anchors)
        selected: dict[str, Path] = {}
        selected_bounds: dict[str, Rect] = {}
        for anchor in alternative.anchors:
            item = self._candidate(snapshot, anchor, candidate_ids.get(anchor))
            rect = self._rect(item.get("bounds"), image, target.anchor_labels.get(anchor, anchor))
            crop_path = staging / f"{anchor}.png"
            image.crop((rect.left, rect.top, rect.right, rect.bottom)).save(crop_path, format="PNG")
            selected[anchor] = crop_path
            selected_bounds[anchor] = rect
            anchors[anchor] = replace(
                anchors[anchor],
                templates=(
                    AnchorTemplateSpec(
                        path=crop_path,
                        minimum_score=0.97,
                        minimum_margin=0.02,
                        coarse_step=1,
                        scale_factors=(scale,),
                        native_scale=scale,
                    ),
                ),
            )

        local_alternative, geometry_checks = self._adaptive_geometry(
            spec_alternative,
            selected_bounds,
            image,
            scale,
        )

        candidate_spec = replace(
            spec,
            anchors=anchors,
            alternatives=(local_alternative,),
        )
        locator = RelativeLocator(candidate_spec)
        locator.set_scale_policy((scale,), (scale,))
        result = locator.locate(image, skip_optional_anchors=True)
        if not result.accepted or len(result.distinct_combinations) > 1 or not result.valid_combinations:
            recognition = {
                "failure_code": result.failure_code,
                "valid_combination_count": len(result.valid_combinations),
                "distinct_combination_count": len(result.distinct_combinations),
            }
            raise RecognitionRepairError(
                "repair_validation_failed",
                "选中的位置关系可以安全适配，但候选裁图重新扫描后没有形成唯一有效组合，未保存任何识别图片。",
                details={
                    "geometry_checks": list(geometry_checks),
                    "recognition": recognition,
                },
            )
        for anchor, expected in selected_bounds.items():
            actual = result.detections.get(anchor)
            if actual is None or actual.bounds is None:
                raise RecognitionRepairError(
                    "repair_validation_failed",
                    "选中的候选裁图无法在原截图中重新定位，未保存任何识别图片。",
                    details={"geometry_checks": list(geometry_checks)},
                )
            if abs(actual.bounds.center.x - expected.center.x) > max(4, expected.width // 2):
                raise RecognitionRepairError(
                    "repair_validation_failed",
                    f"“{target.anchor_labels.get(anchor, anchor)}”重新识别后横向位置发生偏移，未保存任何识别图片。",
                    details={"geometry_checks": list(geometry_checks)},
                )
            if abs(actual.bounds.center.y - expected.center.y) > max(4, expected.height // 2):
                raise RecognitionRepairError(
                    "repair_validation_failed",
                    f"“{target.anchor_labels.get(anchor, anchor)}”重新识别后纵向位置发生偏移，未保存任何识别图片。",
                    details={"geometry_checks": list(geometry_checks)},
                )
        return PreparedRepair(
            snapshot=snapshot,
            target=target,
            alternative=alternative,
            locator_alternative=local_alternative,
            result=result,
            selected=selected,
            percent=percent,
            scale=scale,
            geometry_checks=geometry_checks,
        )

    @staticmethod
    def _preview(prepared: PreparedRepair) -> dict[str, Any]:
        snapshot = prepared.snapshot
        target = prepared.target
        alternative = prepared.alternative
        result = prepared.result
        return {
            "valid": True,
            "message": f"已确认“{target.label}”的{alternative.label}，可以保存为本机识别方式。",
            "target_id": target.target_id,
            "alternative_id": alternative.alternative_id,
            "snapshot_id": str(snapshot.get("id") or ""),
            "scale_percent": prepared.percent,
            "target": {
                "left": result.target.left if result.target else None,
                "top": result.target.top if result.target else None,
                "right": result.target.right if result.target else None,
                "bottom": result.target.bottom if result.target else None,
            },
            "score": round(
                sum(item.score for item in result.detections.values())
                / max(1, len(result.detections)),
                6,
            ),
            "selected_anchors": sorted(prepared.selected),
            "geometry_checks": list(prepared.geometry_checks),
        }

    def validate(self, **kwargs: Any) -> dict[str, Any]:
        staging = Path(tempfile.mkdtemp(prefix=".repair-check-", dir=self.root.parent))
        try:
            prepared = self._prepare_validation(staging=staging, **kwargs)
            return {"ok": True, **self._preview(prepared)}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def save(self, **kwargs: Any) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".repair-save-", dir=self.root))
        destination: Path | None = None
        try:
            prepared = self._prepare_validation(staging=staging, **kwargs)
            snapshot = prepared.snapshot
            target = prepared.target
            alternative = prepared.alternative
            result = prepared.result
            selected = prepared.selected
            percent = prepared.percent
            scale = prepared.scale
            locator_id = load_relative_locator(
                self.locator_root / target.locator_file,
                include_local=False,
            ).locator_id
            revision = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
            destination = self.root / "templates" / locator_id / revision
            destination.mkdir(parents=True, exist_ok=False)
            profile_anchors: dict[str, list[dict[str, Any]]] = {}
            for anchor, source in selected.items():
                output = destination / f"{anchor}.png"
                shutil.copyfile(source, output)
                profile_anchors[anchor] = [
                    {
                        "path": output.relative_to(self.root).as_posix(),
                        "native_scale": scale,
                        "minimum_score": 0.97,
                        "minimum_margin": 0.02,
                        "coarse_step": 1,
                    }
                ]
            manifest = self._manifest()
            manifest["kind"] = "machine_local_template_overlay"
            manifest.setdefault("profiles", {})[locator_id] = {
                "active": True,
                "mode": "additive_overlay",
                "replaces_builtin_templates": False,
                "updated_at": time.time(),
                "source_snapshot_id": str(snapshot.get("id") or ""),
                "target_id": target.target_id,
                "alternative_id": alternative.alternative_id,
                "scale_percent": percent,
                "anchors": profile_anchors,
                "geometry": self._geometry_manifest(prepared.locator_alternative),
                "validated_target": {
                    "left": result.target.left if result.target else None,
                    "top": result.target.top if result.target else None,
                    "right": result.target.right if result.target else None,
                    "bottom": result.target.bottom if result.target else None,
                },
            }
            self._write_manifest(manifest)
            return {
                "ok": True,
                "message": f"{target.label}已保存这组本机识别图片，并通过组合验证。",
                **self.status(),
            }
        except Exception:
            # A failed validation or manifest write must not activate a partial profile.
            if destination is not None and destination.is_dir():
                shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def disable(self, target_id: str) -> dict[str, Any]:
        target = self.targets.get(str(target_id or ""))
        if target is None:
            raise RecognitionRepairError("repair_target_invalid", "无法判断要恢复默认识别方式的位置。")
        locator_id = load_relative_locator(
            self.locator_root / target.locator_file,
            include_local=False,
        ).locator_id
        manifest = self._manifest()
        profile = dict((manifest.get("profiles") or {}).get(locator_id) or {})
        if profile:
            profile["active"] = False
            profile["updated_at"] = time.time()
            manifest.setdefault("profiles", {})[locator_id] = profile
            self._write_manifest(manifest)
        return {"ok": True, "message": f"{target.label}已恢复使用程序默认识别方式。", **self.status()}


__all__ = [
    "RecognitionRepairError",
    "RecognitionRepairManager",
    "REPAIR_TARGETS",
]
