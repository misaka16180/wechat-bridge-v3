"""Read-only visual compatibility probes for the visible WeChat window."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from statistics import median

from PIL import Image, UnidentifiedImageError

from .relative_locator import (
    RelativeLocator,
    RelativeLocatorResult,
    load_relative_locator,
)


MAX_IMPORTED_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_IMPORTED_SCREENSHOT_PIXELS = 40_000_000
MAX_IMPORTED_SCREENSHOT_EDGE = 16_384
SUPPORTED_IMPORTED_SCREENSHOT_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


class ImportedScreenshotError(ValueError):
    """A bounded, user-facing imported screenshot validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class ImportedScreenshot:
    image: Image.Image
    format: str
    size_bytes: int


def validate_imported_screenshot_request(
    *,
    content_length: object,
    scale_percent: object,
    input_source: object,
) -> tuple[int, int, str]:
    """Validate metadata before reading an imported screenshot request body."""

    try:
        size = int(content_length)
    except (TypeError, ValueError) as exc:
        raise ImportedScreenshotError(
            "invalid_screenshot_length",
            "截图文件大小无效。",
        ) from exc
    if size <= 0:
        raise ImportedScreenshotError("empty_screenshot", "导入的截图为空。")
    if size > MAX_IMPORTED_SCREENSHOT_BYTES:
        raise ImportedScreenshotError(
            "screenshot_too_large",
            "截图超过 20 MiB 上限；请改用 PNG、JPEG 或 WebP 压缩后重试。",
        )

    try:
        scale = int(str(scale_percent).strip())
    except (TypeError, ValueError) as exc:
        raise ImportedScreenshotError(
            "invalid_screenshot_scale",
            "截图缩放比例必须是 50% 到 300% 之间的整数。",
        ) from exc
    if not 50 <= scale <= 300:
        raise ImportedScreenshotError(
            "invalid_screenshot_scale",
            "截图缩放比例必须在 50% 到 300% 之间。",
        )

    source = str(input_source or "").strip().lower()
    if source not in {"file", "clipboard"}:
        raise ImportedScreenshotError(
            "invalid_screenshot_source",
            "截图来源只能是外部文件或剪贴板。",
        )
    return size, scale, source


def decode_imported_screenshot(payload: bytes) -> ImportedScreenshot:
    """Decode an imported screenshot without trusting its name or MIME type."""

    size, _scale, _source = validate_imported_screenshot_request(
        content_length=len(payload),
        scale_percent=100,
        input_source="file",
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as opened:
                image_format = str(opened.format or "").upper()
                if image_format not in SUPPORTED_IMPORTED_SCREENSHOT_FORMATS:
                    raise ImportedScreenshotError(
                        "unsupported_screenshot_format",
                        "只支持真实的 PNG、JPEG 或 WebP 截图。",
                    )
                width, height = map(int, opened.size)
                if width <= 0 or height <= 0:
                    raise ImportedScreenshotError(
                        "invalid_screenshot_dimensions",
                        "截图像素尺寸无效。",
                    )
                if (
                    width > MAX_IMPORTED_SCREENSHOT_EDGE
                    or height > MAX_IMPORTED_SCREENSHOT_EDGE
                    or width * height > MAX_IMPORTED_SCREENSHOT_PIXELS
                ):
                    raise ImportedScreenshotError(
                        "screenshot_dimensions_too_large",
                        "截图像素尺寸过大；宽高均需不超过 16384，且总像素不超过 4000 万。",
                    )
                opened.load()
                image = opened.convert("RGB")
    except ImportedScreenshotError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImportedScreenshotError(
            "screenshot_dimensions_too_large",
            "截图像素尺寸过大，已停止解码。",
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImportedScreenshotError(
            "invalid_screenshot",
            "文件不是可正常解码的 PNG、JPEG 或 WebP 截图。",
        ) from exc
    return ImportedScreenshot(image=image, format=image_format, size_bytes=size)


@dataclass(frozen=True)
class VisualCompatibilityProbe:
    check_id: str
    label: str
    result: RelativeLocatorResult


class VisualCompatibilityChecker:
    """Run the same paired-anchor locators without performing any input."""

    CHECKS = (
        ("search_box", "微信搜索框", "search_box_anchors.json"),
        ("chat_input", "聊天输入区域", "chat_input_by_toolbar.json"),
    )

    def __init__(self, locator_root: str | Path | None = None) -> None:
        root = Path(
            locator_root
            or Path(__file__).resolve().parents[1] / "locators"
        )
        self._locators = tuple(
            (
                check_id,
                label,
                RelativeLocator(load_relative_locator(root / filename)),
            )
            for check_id, label, filename in self.CHECKS
        )

    @staticmethod
    def _auto_scale_range(
        minimum_percent: int,
        maximum_percent: int,
        step_percent: int = 5,
    ) -> tuple[float, ...]:
        step = max(1, int(step_percent))
        values = list(range(minimum_percent, maximum_percent + 1, step))
        if not values or values[-1] != maximum_percent:
            values.append(maximum_percent)
        return tuple(round(value / 100.0, 4) for value in values)

    @classmethod
    def scale_policy(
        cls,
        *,
        dpi: int,
        mode: str,
        manual_percent: int,
        auto_min_percent: int,
        auto_max_percent: int,
        auto_step_percent: int = 5,
    ) -> tuple[tuple[float, ...], tuple[float, ...], str]:
        reported = round(max(48, min(288, int(dpi))) / 96.0, 4)
        if mode == "manual":
            preferred = (round(int(manual_percent) / 100.0, 4),)
            return preferred, preferred, f"手动 {manual_percent}%"
        scanned = cls._auto_scale_range(
            int(auto_min_percent),
            int(auto_max_percent),
            int(auto_step_percent),
        )
        fallback = tuple(dict.fromkeys((reported, *scanned)))
        return (
            (reported,),
            fallback,
            (
                f"Windows {dpi} DPI / {reported * 100:.0f}%；"
                f"失败后按 {auto_step_percent}% 间隔扫描 "
                f"{auto_min_percent}%–{auto_max_percent}%"
            ),
        )

    def run(
        self,
        image: Image.Image,
        *,
        preferred_scales: tuple[float, ...],
        fallback_scales: tuple[float, ...],
        check_ids: tuple[str, ...] | None = None,
    ) -> tuple[VisualCompatibilityProbe, ...]:
        """Run only the requested probes, in the declared UI order.

        Keeping the selection here makes the compatibility pipeline explicit:
        the caller can collect scale evidence first and then run the required
        checks in order, without every stage silently re-running unrelated
        locators.
        """

        selected = set(check_ids) if check_ids is not None else None
        probes: list[VisualCompatibilityProbe] = []
        for check_id, label, locator in self._locators:
            if selected is not None and check_id not in selected:
                continue
            locator.set_scale_policy(preferred_scales, fallback_scales)
            result = locator.locate_with_diagnostics(
                image,
                skip_optional_anchors=True,
            )
            probes.append(VisualCompatibilityProbe(check_id, label, result))
        return tuple(probes)

    @staticmethod
    def assess_scale(
        probes: tuple[VisualCompatibilityProbe, ...],
        *,
        reported_scale: float,
        attempted_scales: tuple[float, ...],
    ) -> dict[str, object]:
        """Separate the reported DPI fact from visual scale evidence."""

        passed = [probe for probe in probes if probe.result.accepted]
        accepted_scales = [
            float(detection.scale)
            for probe in passed
            for detection in probe.result.detections.values()
            if detection.accepted and detection.bounds is not None
        ]
        qualified_evidence = [
            detection
            for probe in probes
            for values in (probe.result.anchor_candidates or {}).values()
            for detection in values
            if detection.accepted and detection.bounds is not None
        ]
        diagnostic_evidence = [
            detection
            for probe in probes
            for values in (probe.result.diagnostic_candidates or {}).values()
            for detection in values
            if detection.bounds is not None
        ]

        reported_percent = int(round(float(reported_scale) * 100))
        attempted = tuple(
            int(round(float(value) * 100))
            for value in dict.fromkeys(attempted_scales)
        )
        selected: float | None = None
        status = "unresolved"
        message = (
            "没有足够的成组元素证据确认有效图像比例；最高分候选只能供人工复查。"
        )

        if accepted_scales:
            selected = float(median(accepted_scales))
            tolerance = max(0.04, selected * 0.06)
            consistent = max(accepted_scales) - min(accepted_scales) <= tolerance
            if len(passed) == len(probes) and consistent:
                status = "confirmed"
                message = (
                    f"成组元素在约 {selected * 100:.0f}% 的图像比例下均能唯一定位。"
                )
            elif not consistent:
                status = "conflict"
                message = (
                    "不同位置给出的图像比例不一致，不能把其中一个最高分结果当作正确比例。"
                )
            else:
                status = "partial"
                message = (
                    f"约 {selected * 100:.0f}% 已获得部分成组证据，但仍不足以确认整张界面兼容。"
                )
        elif len(qualified_evidence) >= 2:
            scales = [float(item.scale) for item in qualified_evidence]
            selected = float(median(scales))
            tolerance = max(0.04, selected * 0.06)
            if max(scales) - min(scales) <= tolerance:
                status = "partial"
                message = (
                    f"约 {selected * 100:.0f}% 找到了多个达标元素，但它们尚未组成可靠目标。"
                )

        best = max(diagnostic_evidence, key=lambda item: item.score, default=None)
        suggested = selected if selected is not None else (
            float(best.scale) if best is not None else None
        )
        return {
            "status": status,
            "reported_scale_percent": reported_percent,
            "effective_scale_percent": (
                int(round(selected * 100)) if status == "confirmed" and selected else None
            ),
            "suggested_scale_percent": (
                int(round(suggested * 100)) if suggested is not None else None
            ),
            "best_candidate_score": (
                round(float(best.score), 6) if best is not None else None
            ),
            "accepted_check_count": len(passed),
            "check_count": len(probes),
            "evidence_count": len(qualified_evidence),
            "attempted_scale_percents": list(attempted),
            "message": message,
        }


__all__ = [
    "ImportedScreenshot",
    "ImportedScreenshotError",
    "MAX_IMPORTED_SCREENSHOT_BYTES",
    "MAX_IMPORTED_SCREENSHOT_EDGE",
    "MAX_IMPORTED_SCREENSHOT_PIXELS",
    "SUPPORTED_IMPORTED_SCREENSHOT_FORMATS",
    "VisualCompatibilityChecker",
    "VisualCompatibilityProbe",
    "decode_imported_screenshot",
    "validate_imported_screenshot_request",
]
