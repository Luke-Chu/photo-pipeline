from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from PIL import Image, ExifTags
import piexif


EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}


def _to_json_safe(value: Any) -> Any:
    """
    递归把 EXIF 中不可 JSON 序列化的对象转换为可序列化对象
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            return str(value)

    # Pillow / EXIF 常见分数对象，例如 IFDRational
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        try:
            if value.denominator == 0:
                return None
            return round(float(value.numerator) / float(value.denominator), 6)
        except Exception:
            return str(value)

    # 普通 tuple/list 递归处理
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]

    # dict 递归处理
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}

    # 兜底
    return str(value)


def _rational_to_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            numerator, denominator = value
            if denominator == 0:
                return None
            return round(numerator / denominator, 4)
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            if value.denominator == 0:
                return None
            return round(value.numerator / value.denominator, 4)
        return float(value)
    except Exception:
        return None


def _format_aperture(value: Any) -> Optional[str]:
    f = _rational_to_float(value)
    if f is None:
        return None
    return f"f/{f:.1f}"


def _format_shutter(value: Any) -> Optional[str]:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            numerator, denominator = value
            if denominator == 0:
                return None
            if numerator < denominator:
                return f"{numerator}/{denominator}"
            return f"{round(numerator / denominator, 2)}s"
        f = _rational_to_float(value)
        if f is None:
            return None
        if f < 1:
            return f"1/{round(1 / f)}"
        return f"{round(f, 2)}s"
    except Exception:
        return None


def _format_focal_length(value: Any) -> Optional[str]:
    f = _rational_to_float(value)
    if f is None:
        return None
    if abs(f - round(f)) < 0.05:
        return f"{int(round(f))}mm"
    return f"{f:.1f}mm"


def _parse_exif_datetime(value: Any) -> Optional[str]:
    value = _to_json_safe(value)
    if not value:
        return None
    if isinstance(value, str):
        # EXIF 常见格式: 2024:10:15 18:42:10
        return value.replace(":", "-", 2)
    return None


def _build_raw_exif_dict(exif_data: Dict[int, Any]) -> Dict[str, Any]:
    raw = {}
    for tag_id, val in exif_data.items():
        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
        raw[tag_name] = _to_json_safe(val)
    return raw


def extract_image_metadata(image_path: Path, default_author: str = "Luke Chu") -> Dict[str, Any]:
    """
    提取图片基础信息 + EXIF 标准字段 + 原始 EXIF
    """
    metadata: Dict[str, Any] = {
        "original_filename": image_path.name,
        "author": default_author,
        "shot_time": None,
        "width": None,
        "height": None,
        "resolution": None,
        "camera_model": None,
        "lens_model": None,
        "aperture": None,
        "shutter_speed": None,
        "iso": None,
        "focal_length": None,
        "focal_length_35mm": None,
        "metering_mode": None,
        "exposure_program": None,
        "white_balance": None,
        "color_temperature": None,
        "color_space": None,
        "bit_depth": None,
        "raw_exif": {},
    }

    with Image.open(image_path) as img:
        metadata["width"], metadata["height"] = img.size
        metadata["resolution"] = f"{img.width}x{img.height}"
        metadata["bit_depth"] = img.mode
        metadata["color_space"] = img.mode

        exif_data = img.getexif()
        if exif_data:
            metadata["raw_exif"] = _build_raw_exif_dict(dict(exif_data))

        try:
            piexif_data = piexif.load(img.info.get("exif", b""))
        except Exception:
            piexif_data = {}

    # 优先用 piexif 做更稳定的解析
    if piexif_data:
        zeroth = piexif_data.get("0th", {})
        exif_ifd = piexif_data.get("Exif", {})

        metadata["camera_model"] = _to_json_safe(zeroth.get(piexif.ImageIFD.Model))
        metadata["author"] = _to_json_safe(zeroth.get(piexif.ImageIFD.Artist)) or default_author

        metadata["shot_time"] = _parse_exif_datetime(
            exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
            or zeroth.get(piexif.ImageIFD.DateTime)
        )

        metadata["lens_model"] = _to_json_safe(exif_ifd.get(piexif.ExifIFD.LensModel))
        metadata["aperture"] = _format_aperture(exif_ifd.get(piexif.ExifIFD.FNumber))
        metadata["shutter_speed"] = _format_shutter(exif_ifd.get(piexif.ExifIFD.ExposureTime))
        metadata["iso"] = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
        metadata["focal_length"] = _format_focal_length(exif_ifd.get(piexif.ExifIFD.FocalLength))
        metadata["focal_length_35mm"] = _format_focal_length(
            exif_ifd.get(piexif.ExifIFD.FocalLengthIn35mmFilm)
        )
        metadata["metering_mode"] = exif_ifd.get(piexif.ExifIFD.MeteringMode)
        metadata["exposure_program"] = exif_ifd.get(piexif.ExifIFD.ExposureProgram)
        metadata["white_balance"] = exif_ifd.get(piexif.ExifIFD.WhiteBalance)

        # 有些图没有色温
        color_temp = exif_ifd.get(piexif.ExifIFD.Temperature)
        metadata["color_temperature"] = str(color_temp) if color_temp is not None else None

    return metadata