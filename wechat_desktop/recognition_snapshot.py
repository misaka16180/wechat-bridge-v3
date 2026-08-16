"""Persistent, authenticated recognition evidence for v3 visual automation."""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from PIL import Image

from .models import Rect
from .relative_locator import (
    DIAGNOSTIC_CANDIDATE_LIMIT,
    AnchorDetection,
    RelativeLocatorCombination,
    RelativeLocatorResult,
    draw_combination_overlay,
    draw_debug_overlay,
)


log = logging.getLogger("wechat_automation.snapshot")
_local = threading.local()


def _rect(value: Rect | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        "left": value.left,
        "top": value.top,
        "right": value.right,
        "bottom": value.bottom,
        "width": value.width,
        "height": value.height,
    }


def _detection(value: AnchorDetection) -> dict[str, Any]:
    return {
        "anchor_id": value.anchor_id,
        "template": value.template.name if value.template is not None else None,
        "bounds": _rect(value.bounds),
        "score": round(value.score, 6),
        "second_score": round(value.second_score, 6),
        "accepted": bool(value.accepted),
        "scale": round(value.scale, 4),
    }


def _effective_anchor_best(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recover real best attempts for snapshots written by older v3 builds."""

    stored = dict(metadata.get("anchor_best") or {})
    review = dict(metadata.get("review_candidates") or {})
    accepted = dict(metadata.get("anchor_candidates") or {})
    anchor_ids = tuple(dict.fromkeys((*stored, *review, *accepted)))
    result: dict[str, dict[str, Any]] = {}
    for anchor_id in anchor_ids:
        current = stored.get(anchor_id)
        if (
            isinstance(current, dict)
            and current.get("template")
            and current.get("bounds")
        ):
            result[str(anchor_id)] = dict(current)
            continue
        candidates = review.get(anchor_id) or accepted.get(anchor_id) or ()
        viable = [
            item
            for item in candidates
            if isinstance(item, dict) and item.get("template") and item.get("bounds")
        ]
        if viable:
            result[str(anchor_id)] = dict(
                max(viable, key=lambda item: float(item.get("score") or 0.0))
            )
    return result


def _combination(value: RelativeLocatorCombination) -> dict[str, Any]:
    return {
        "id": value.combination_id,
        "alternative": value.alternative_id,
        "score": round(value.score, 6),
        "target": _rect(value.target),
        "click_bounds": _rect(value.click_bounds),
        "reference_bounds": _rect(value.reference_bounds),
        "anchors": [
            _detection(value.detections[anchor_id])
            for anchor_id in value.used_anchor_ids
        ],
    }


def _reason(result: RelativeLocatorResult) -> tuple[str, str]:
    if result.accepted:
        supports = max(1, len(result.valid_combinations))
        return (
            "located",
            f"定位成功：{supports} 组锚点组合共同指向 1 个目标区域。",
        )
    if result.failure_code == "ambiguous_combinations":
        return (
            "ambiguous_combinations",
            (
                f"定位歧义：检测到 {len(result.distinct_combinations)} 个指向不同区域的"
                "有效元素组合。为避免误点击，本次定位已停止。"
            ),
        )
    if result.failure_code == "no_valid_combination":
        return (
            "no_valid_combination",
            "找到了图像候选，但没有任何候选组合同时满足距离、边缘和尺寸条件。",
        )
    if result.failure_code == "cached_layout_invalid":
        return (
            "cached_layout_invalid",
            "缓存位置附近的组合验证没有通过，将回退到完整识别。",
        )
    return (
        "anchor_candidates_missing",
        "至少一个必需定位点没有找到达到阈值的图像候选。",
    )


@dataclass(frozen=True)
class RecognitionRun:
    store: "RecognitionSnapshotStore"
    run_id: str
    source: str
    capture_success: bool


class RecognitionSnapshotStore:
    """Save bounded PNG/JSON evidence and expose it without absolute paths."""

    def __init__(self, root: str | Path, *, max_snapshots: int = 120) -> None:
        self.root = Path(root).resolve()
        self.max_snapshots = max(10, int(max_snapshots))
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        candidate = str(value or "")
        if not candidate or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in candidate):
            raise KeyError("识别快照编号无效。")
        return candidate

    @staticmethod
    def _public(metadata: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = str(metadata["id"])
        result = dict(metadata)
        result["anchor_best"] = _effective_anchor_best(metadata)
        files = dict(result.pop("files", {}))
        result["images"] = {
            name: f"/api/recognition-snapshots/{snapshot_id}/images/{name}"
            for name in files
        }
        return result

    def _directories(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )

    def _trim(self) -> None:
        for directory in self._directories()[self.max_snapshots :]:
            shutil.rmtree(directory, ignore_errors=True)

    def _write_review_candidates(
        self,
        directory: Path,
        image: Image.Image,
        candidate_map: dict[str, tuple[AnchorDetection, ...]],
        files: dict[str, str],
        *,
        generation: str = "",
    ) -> dict[str, list[dict[str, Any]]]:
        review_candidates: dict[str, list[dict[str, Any]]] = {}
        key_prefix = f"candidate_{generation}_" if generation else "candidate_"
        file_prefix = f"candidate-{generation}-" if generation else "candidate-"
        id_prefix = f"candidate-{generation}-" if generation else "candidate-"
        for anchor_id, values in candidate_map.items():
            safe_anchor = self._diagnostic_name(anchor_id)
            review_items: list[dict[str, Any]] = []
            for index, detection in enumerate(values, start=1):
                bounds = detection.bounds
                if bounds is None:
                    continue
                left = max(0, int(bounds.left))
                top = max(0, int(bounds.top))
                right = min(image.width, int(bounds.right))
                bottom = min(image.height, int(bounds.bottom))
                if right <= left or bottom <= top:
                    continue
                image_name = f"{key_prefix}{safe_anchor}_{index}"
                filename = f"{file_prefix}{safe_anchor}-{index}.png"
                image.crop((left, top, right, bottom)).convert("RGB").save(
                    directory / filename,
                    format="PNG",
                )
                files[image_name] = filename
                payload = _detection(detection)
                payload.update(
                    {
                        "candidate_id": (
                            f"{id_prefix}{safe_anchor}-{index}-"
                            f"{secrets.token_hex(4)}"
                        ),
                        "image_name": image_name,
                    }
                )
                review_items.append(payload)
            review_candidates[str(anchor_id)] = review_items
        return review_candidates

    def save(
        self,
        image: Image.Image,
        result: RelativeLocatorResult,
        *,
        run_id: str,
        source: str,
        label: str,
        operation: str = "",
        error_message: str = "",
        overview: Image.Image | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        stamp = datetime.fromtimestamp(now).astimezone()
        snapshot_id = stamp.strftime("%Y%m%d-%H%M%S-") + f"{stamp.microsecond // 1000:03d}-{secrets.token_hex(3)}"
        directory = self.root / snapshot_id
        failure_code, reason = _reason(result)
        outcome = "success" if result.accepted else "failure"
        files: dict[str, str] = {
            "original": "original.png",
            "overview": "overview.png",
        }
        combinations = list(result.valid_combinations)
        try:
            with self._lock:
                directory.mkdir(parents=True, exist_ok=False)
                image.convert("RGB").save(directory / files["original"], format="PNG")
                (
                    overview
                    if overview is not None
                    else draw_debug_overlay(image, result)
                ).save(
                    directory / files["overview"],
                    format="PNG",
                )
                for index, combination in enumerate(combinations[:12], start=1):
                    key = f"combination_{index}"
                    filename = f"combination-{index}.png"
                    files[key] = filename
                    draw_combination_overlay(image, combination).save(
                        directory / filename,
                        format="PNG",
                    )
                candidate_map = result.anchor_candidates or {}
                diagnostic_map = result.diagnostic_candidates or candidate_map
                # Keep every bounded diagnostic candidate. Runtime matching
                # remains small; this evidence set is specifically for human
                # review and can later be regenerated with a user threshold.
                review_candidates = self._write_review_candidates(
                    directory,
                    image,
                    diagnostic_map,
                    files,
                )
                best_attempts: dict[str, AnchorDetection] = {}
                anchor_ids = tuple(
                    dict.fromkeys((*result.detections, *diagnostic_map))
                )
                for anchor_id in anchor_ids:
                    detection = result.detections.get(anchor_id)
                    if (
                        detection is None
                        or detection.template is None
                        or detection.bounds is None
                    ):
                        diagnostic_values = diagnostic_map.get(anchor_id, ())
                        detection = max(
                            diagnostic_values,
                            key=lambda item: item.score,
                            default=None,
                        )
                    if (
                        detection is not None
                        and detection.template is not None
                        and detection.bounds is not None
                    ):
                        best_attempts[anchor_id] = detection
                metadata = {
                    "id": snapshot_id,
                    "run_id": str(run_id),
                    "source": str(source),
                    "label": str(label),
                    "operation": str(operation),
                    "created_at": now,
                    "created_at_text": stamp.isoformat(timespec="milliseconds"),
                    "outcome": outcome,
                    "reason_code": failure_code,
                    "reason": str(error_message or reason),
                    "locator_failure_code": result.failure_code,
                    "alternative": result.alternative_id,
                    "image_size": {"width": image.width, "height": image.height},
                    "target": _rect(result.target),
                    "click_bounds": _rect(result.click_bounds),
                    "candidate_counts": {
                        anchor_id: len(values)
                        for anchor_id, values in candidate_map.items()
                    },
                    "review_candidate_counts": {
                        anchor_id: len(values)
                        for anchor_id, values in review_candidates.items()
                    },
                    "review_candidate_score_floor": (
                        0.0 if result.diagnostic_candidates is not None else None
                    ),
                    "review_candidate_limit": (
                        DIAGNOSTIC_CANDIDATE_LIMIT
                        if result.diagnostic_candidates is not None
                        else max((len(values) for values in review_candidates.values()), default=0)
                    ),
                    "review_candidate_limit_reached": {
                        anchor_id: (
                            result.diagnostic_candidates is not None
                            and len(values) >= DIAGNOSTIC_CANDIDATE_LIMIT
                        )
                        for anchor_id, values in review_candidates.items()
                    },
                    "anchor_candidates": {
                        anchor_id: [_detection(item) for item in values]
                        for anchor_id, values in candidate_map.items()
                    },
                    "anchor_best": {
                        anchor_id: _detection(detection)
                        for anchor_id, detection in best_attempts.items()
                    },
                    "review_candidates": review_candidates,
                    "valid_combination_count": len(combinations),
                    "distinct_target_count": len(result.distinct_combinations),
                    "combinations": [_combination(item) for item in combinations],
                    "combination_images_limited": len(combinations) > 12,
                    "rejected_alternatives": list(result.rejected_alternatives),
                    "files": files,
                }
                if extra_metadata:
                    metadata["extra"] = dict(extra_metadata)
                (directory / "snapshot.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._trim()
                return self._public(metadata)
        except Exception as exc:
            shutil.rmtree(directory, ignore_errors=True)
            log.warning("保存识别快照失败：%s", exc)
            return None

    def list(
        self,
        *,
        outcome: str = "",
        source: str = "",
        run_id: str = "",
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self._lock:
            for directory in self._directories():
                try:
                    metadata = json.loads(
                        (directory / "snapshot.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if outcome and metadata.get("outcome") != outcome:
                    continue
                if source and metadata.get("source") != source:
                    continue
                if run_id and metadata.get("run_id") != run_id:
                    continue
                result.append(self._public(metadata))
                if len(result) >= max(1, min(int(limit), 120)):
                    break
        return result

    def replace_review_candidates(
        self,
        snapshot_id: str,
        candidate_map: dict[str, tuple[AnchorDetection, ...]],
        *,
        score_floor: float,
        candidate_limit: int,
    ) -> dict[str, Any]:
        """Atomically replace derived candidate crops for one saved screenshot."""

        safe_id = self._safe_id(snapshot_id)
        directory = self.root / safe_id
        metadata_path = directory / "snapshot.json"
        generation = secrets.token_hex(4)
        created_files: dict[str, str] = {}
        with self._lock:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                files = dict(metadata.get("files") or {})
                original_name = str(files.get("original") or "")
                original_path = (directory / original_name).resolve()
            except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
                raise KeyError("识别快照不存在或已经过期。") from exc
            if (
                not original_name
                or original_path.parent != directory.resolve()
                or not original_path.is_file()
            ):
                raise KeyError("识别快照原图不存在。")

            old_candidate_files = {
                name: filename
                for name, filename in files.items()
                if str(name).startswith("candidate_")
            }
            retained_files = {
                name: filename
                for name, filename in files.items()
                if name not in old_candidate_files
            }
            try:
                with Image.open(original_path) as opened:
                    image = opened.convert("RGB")
                review_candidates = self._write_review_candidates(
                    directory,
                    image,
                    candidate_map,
                    created_files,
                    generation=generation,
                )
                metadata["review_candidates"] = review_candidates
                metadata["review_candidate_counts"] = {
                    anchor_id: len(values)
                    for anchor_id, values in review_candidates.items()
                }
                metadata["review_candidate_score_floor"] = round(
                    float(score_floor), 4
                )
                metadata["review_candidate_limit"] = int(candidate_limit)
                metadata["review_candidate_limit_reached"] = {
                    anchor_id: len(values) >= int(candidate_limit)
                    for anchor_id, values in review_candidates.items()
                }
                metadata["files"] = {**retained_files, **created_files}
                temporary = metadata_path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(metadata_path)
            except Exception:
                for filename in created_files.values():
                    try:
                        candidate_path = (directory / filename).resolve()
                        if candidate_path.parent == directory.resolve():
                            candidate_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

            active_files = set(created_files.values())
            for filename in old_candidate_files.values():
                if filename in active_files:
                    continue
                try:
                    candidate_path = (directory / filename).resolve()
                    if candidate_path.parent == directory.resolve():
                        candidate_path.unlink(missing_ok=True)
                except OSError:
                    log.warning("无法清理旧识别候选裁图：%s", filename)
            return self._public(metadata)

    def image_path(self, snapshot_id: str, image_name: str) -> Path:
        safe_id = self._safe_id(snapshot_id)
        safe_name = self._safe_id(image_name)
        metadata_path = self.root / safe_id / "snapshot.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            filename = str(metadata.get("files", {}).get(safe_name) or "")
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError("识别快照不存在或已经过期。") from exc
        path = (metadata_path.parent / filename).resolve()
        if not filename or path.parent != metadata_path.parent.resolve() or not path.is_file():
            raise KeyError("识别快照图片不存在。")
        return path

    @staticmethod
    def _diagnostic_name(value: str) -> str:
        result = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in str(value or "")
        ).strip("_")
        return result[:64] or "anchor"

    def diagnostic_package(self, snapshot_id: str) -> bytes:
        """Return a privacy-minimised ZIP without full-window screenshots."""

        safe_id = self._safe_id(snapshot_id)
        metadata_path = self.root / safe_id / "snapshot.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            original_name = str(metadata.get("files", {}).get("original") or "")
            original_path = (metadata_path.parent / original_name).resolve()
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError("识别快照不存在或已经过期。") from exc
        if (
            not original_name
            or original_path.parent != metadata_path.parent.resolve()
            or not original_path.is_file()
        ):
            raise KeyError("识别快照原图不存在。")

        selected = _effective_anchor_best(metadata)
        if not selected:
            for anchor_id, candidates in dict(
                metadata.get("anchor_candidates") or {}
            ).items():
                if candidates:
                    selected[str(anchor_id)] = candidates[0]

        safe_metadata = {
            key: metadata.get(key)
            for key in (
                "id",
                "run_id",
                "source",
                "label",
                "operation",
                "created_at_text",
                "outcome",
                "reason_code",
                "reason",
                "locator_failure_code",
                "alternative",
                "image_size",
                "target",
                "click_bounds",
                "candidate_counts",
                "anchor_candidates",
                "anchor_best",
                "review_candidates",
                "review_candidate_counts",
                "review_candidate_score_floor",
                "review_candidate_limit",
                "review_candidate_limit_reached",
                "valid_combination_count",
                "distinct_target_count",
                "combinations",
                "rejected_alternatives",
                "extra",
            )
            if key in metadata
        }
        safe_metadata["privacy"] = {
            "full_screenshot_included": False,
            "content": "JSON metadata and small anchor crops only",
        }

        output = BytesIO()
        with Image.open(original_path) as original, zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "diagnostic.json",
                json.dumps(safe_metadata, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "README.txt",
                (
                    "WeChat Bridge v3 visual diagnostic package\n\n"
                    "This privacy-minimised package excludes original.png, overview.png "
                    "and every full-window combination image. It contains only locator "
                    "metadata and small crops around each anchor's best attempt.\n"
                ),
            )
            for index, (anchor_id, detection) in enumerate(selected.items(), start=1):
                bounds = dict((detection or {}).get("bounds") or {})
                try:
                    left = max(0, int(bounds["left"]) - 8)
                    top = max(0, int(bounds["top"]) - 8)
                    right = min(original.width, int(bounds["right"]) + 8)
                    bottom = min(original.height, int(bounds["bottom"]) + 8)
                except (KeyError, TypeError, ValueError):
                    continue
                if right <= left or bottom <= top:
                    continue
                crop = original.crop((left, top, right, bottom)).convert("RGB")
                encoded = BytesIO()
                crop.save(encoded, format="PNG")
                archive.writestr(
                    f"anchors/{index:02d}-{self._diagnostic_name(anchor_id)}.png",
                    encoded.getvalue(),
                )
        return output.getvalue()


@contextmanager
def recognition_run(
    store: RecognitionSnapshotStore,
    *,
    run_id: str,
    source: str,
    capture_success: bool,
) -> Iterator[RecognitionRun]:
    previous = getattr(_local, "run", None)
    current = RecognitionRun(store, str(run_id), str(source), bool(capture_success))
    _local.run = current
    try:
        yield current
    finally:
        _local.run = previous


@contextmanager
def recognition_run_if_missing(
    store: RecognitionSnapshotStore,
    *,
    run_id: str,
    source: str,
    capture_success: bool,
) -> Iterator[RecognitionRun]:
    current = getattr(_local, "run", None)
    if current is not None:
        yield current
        return
    with recognition_run(
        store,
        run_id=run_id,
        source=source,
        capture_success=capture_success,
    ) as created:
        yield created


def record_recognition_snapshot(
    image: Image.Image,
    result: RelativeLocatorResult,
    *,
    label: str,
    operation: str = "",
    force: bool = False,
    error_message: str = "",
    overview: Image.Image | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current: RecognitionRun | None = getattr(_local, "run", None)
    if current is None:
        return None
    if result.accepted and not current.capture_success and not force:
        return None
    return current.store.save(
        image,
        result,
        run_id=current.run_id,
        source=current.source,
        label=label,
        operation=operation,
        error_message=error_message,
        overview=overview,
        extra_metadata=extra_metadata,
    )


__all__ = [
    "RecognitionSnapshotStore",
    "record_recognition_snapshot",
    "recognition_run",
    "recognition_run_if_missing",
]
