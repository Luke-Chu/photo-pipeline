from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional
from PIL import Image, ExifTags
import piexif


EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
RATING_TAG_ID = 18246
RATING_PERCENT_TAG_ID = 18249
TEXT_EXIF_TAGS = {
    "Make",
    "Model",
    "Artist",
    "ImageDescription",
    "XPTitle",
    "XPComment",
    "XPSubject",
    "XPKeywords",
    "XPAuthor",
    "UserComment",
}


def _clean_text_value(text: str) -> str:
    # Remove NUL and non-printable control chars frequently seen in EXIF text.
    cleaned_chars = []
    for ch in text:
        code = ord(ch)
        if ch == "\x00":
            continue
        if code < 32 and ch not in ("\t", "\n", "\r"):
            continue
        cleaned_chars.append(ch)
    return "".join(cleaned_chars).strip()


def _text_quality_score(text: str) -> float:
    if not text:
        return -1e9

    score = 0.0
    for ch in text:
        code = ord(ch)
        if ch == "\ufffd":
            score -= 6.0
        elif code < 32 and ch not in "\t\r\n":
            score -= 4.0
        elif 0x4E00 <= code <= 0x9FFF:
            score += 3.5
        elif 32 <= code <= 126:
            score += 1.0
        elif 0x00A0 <= code <= 0x024F:
            score += 0.2
        else:
            score += 0.5
    return score


def _repair_mojibake_text(text: str) -> str:
    original = _clean_text_value(text)
    candidates = [original]

    try:
        candidates.append(original.encode("latin1").decode("utf-8"))
    except Exception:
        pass

    cleaned = [_clean_text_value(c) for c in candidates if c]
    if not cleaned:
        return original

    return max(cleaned, key=_text_quality_score)


def _decode_text_bytes(data: bytes, tag_name: str) -> str:
    payload = data
    preferred_encodings = []

    if tag_name == "UserComment" and len(data) >= 8:
        prefix = data[:8]
        if prefix.startswith(b"ASCII"):
            payload = data[8:]
            preferred_encodings = ["ascii", "utf-8", "gb18030"]
        elif prefix.startswith(b"UNICODE"):
            payload = data[8:]
            preferred_encodings = ["utf-16", "utf-16le", "utf-16be", "utf-8", "gb18030"]
        elif prefix.startswith(b"JIS"):
            payload = data[8:]
            preferred_encodings = ["shift_jis", "euc_jp", "utf-8"]

    # XPTitle/XPComment/XPSubject/XPKeywords are usually UTF-16LE.
    if tag_name.startswith("XP"):
        preferred_encodings = ["utf-16le", "utf-16be", "utf-8", "gb18030"] + preferred_encodings

    tried = set()
    encodings = preferred_encodings + ["utf-8", "utf-16le", "utf-16be", "gb18030", "gbk", "latin1"]
    candidates = []
    for enc in encodings:
        if enc in tried:
            continue
        tried.add(enc)
        try:
            decoded = payload.decode(enc)
        except Exception:
            continue
        decoded = _repair_mojibake_text(decoded)
        if decoded:
            candidates.append(decoded)

    if not candidates:
        return _clean_text_value(str(data))

    return max(candidates, key=_text_quality_score)


def _decode_exif_text_value(value: Any, tag_name: str) -> Any:
    if value is None:
        return None

    if isinstance(value, str):
        return _repair_mojibake_text(value)

    if isinstance(value, bytes):
        return _decode_text_bytes(value, tag_name)

    if isinstance(value, (list, tuple)):
        if all(isinstance(v, int) and 0 <= v <= 255 for v in value):
            try:
                return _decode_text_bytes(bytes(value), tag_name)
            except Exception:
                return _to_json_safe(value)

    return _to_json_safe(value)


def _to_json_safe(value: Any) -> Any:
    """
    递归把 EXIF 中不可 JSON 序列化的对象转换为可序列化对象
    """
    if value is None:
        return None

    if isinstance(value, str):
        return _repair_mojibake_text(value)

    if isinstance(value, (int, float, bool)):
        return value

    if isinstance(value, bytes):
        return _decode_text_bytes(value, tag_name="")

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


def _format_exposure_compensation(value: Any) -> Optional[str]:
    f = _rational_to_float(value)
    if f is None:
        return None
    if abs(f) < 1e-9:
        return "0EV"
    if abs(f - round(f)) < 0.01:
        return f"{f:+.0f}EV"
    return f"{f:+.1f}EV"


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
        if tag_name in TEXT_EXIF_TAGS:
            raw[tag_name] = _decode_exif_text_value(val, tag_name)
        else:
            raw[tag_name] = _to_json_safe(val)
    return raw


def _parse_int_value(value: Any) -> Optional[int]:
    safe = _to_json_safe(value)
    if safe is None:
        return None
    try:
        return int(safe)
    except Exception:
        return None


def _extract_rating_from_exif_dict(exif_dict: Dict[Any, Any]) -> tuple[Optional[int], Optional[int]]:
    rating = None
    rating_percent = None

    if RATING_TAG_ID in exif_dict:
        rating = _parse_int_value(exif_dict.get(RATING_TAG_ID))
    if RATING_PERCENT_TAG_ID in exif_dict:
        rating_percent = _parse_int_value(exif_dict.get(RATING_PERCENT_TAG_ID))

    for key, value in exif_dict.items():
        key_text = str(key).strip().lower()
        if key_text == "rating" and rating is None:
            rating = _parse_int_value(value)
        elif key_text == "ratingpercent" and rating_percent is None:
            rating_percent = _parse_int_value(value)

    if rating is None and rating_percent is not None:
        if rating_percent <= 5:
            rating = rating_percent
        else:
            rating = round(rating_percent / 20)

    return rating, rating_percent


def _extract_rating_from_xmp_text(xmp_text: str) -> tuple[Optional[int], Optional[int]]:
    patterns = [
        r'xmp:Rating\s*=\s*"(-?\d+)"',
        r"<xmp:Rating>\s*(-?\d+)\s*</xmp:Rating>",
        r"MicrosoftPhoto:Rating\s*=\s*\"(-?\d+)\"",
        r"<MicrosoftPhoto:Rating>\s*(-?\d+)\s*</MicrosoftPhoto:Rating>",
    ]
    percent_patterns = [
        r"MicrosoftPhoto:RatingPercent\s*=\s*\"(-?\d+)\"",
        r"<MicrosoftPhoto:RatingPercent>\s*(-?\d+)\s*</MicrosoftPhoto:RatingPercent>",
    ]

    rating = None
    rating_percent = None
    for pattern in patterns:
        match = re.search(pattern, xmp_text, flags=re.IGNORECASE)
        if match:
            try:
                rating = int(match.group(1))
                break
            except Exception:
                continue

    for pattern in percent_patterns:
        match = re.search(pattern, xmp_text, flags=re.IGNORECASE)
        if match:
            try:
                rating_percent = int(match.group(1))
                break
            except Exception:
                continue

    if rating is None and rating_percent is not None:
        if rating_percent <= 5:
            rating = rating_percent
        else:
            rating = round(rating_percent / 20)

    return rating, rating_percent


def _extract_rating_from_file_xmp(image_path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        raw_bytes = image_path.read_bytes()
    except Exception:
        return None, None

    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return None, None

    return _extract_rating_from_xmp_text(text)


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
        "exposure_compensation": None,
        "iso": None,
        "focal_length": None,
        "focal_length_35mm": None,
        "metering_mode": None,
        "exposure_program": None,
        "white_balance": None,
        "flash": None,
        "color_temperature": None,
        "color_space": None,
        "bit_depth": None,
        "raw_exif": {},
    }
    rating: Optional[int] = None
    rating_percent: Optional[int] = None

    with Image.open(image_path) as img:
        metadata["width"], metadata["height"] = img.size
        metadata["resolution"] = f"{img.width}x{img.height}"
        metadata["bit_depth"] = img.mode
        metadata["color_space"] = img.mode

        exif_data = img.getexif()
        exif_dict = dict(exif_data) if exif_data else {}
        if exif_data:
            metadata["raw_exif"] = _build_raw_exif_dict(exif_dict)
        rating, rating_percent = _extract_rating_from_exif_dict(exif_dict)

        try:
            piexif_data = piexif.load(img.info.get("exif", b""))
        except Exception:
            piexif_data = {}

    # 优先用 piexif 做更稳定的解析
    if piexif_data:
        zeroth = piexif_data.get("0th", {})
        exif_ifd = piexif_data.get("Exif", {})
        piexif_rating, piexif_rating_percent = _extract_rating_from_exif_dict(zeroth)
        if rating is None:
            rating = piexif_rating
        if rating_percent is None:
            rating_percent = piexif_rating_percent

        metadata["camera_model"] = _to_json_safe(zeroth.get(piexif.ImageIFD.Model))
        metadata["author"] = _to_json_safe(zeroth.get(piexif.ImageIFD.Artist)) or default_author

        metadata["shot_time"] = _parse_exif_datetime(
            exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
            or zeroth.get(piexif.ImageIFD.DateTime)
        )

        metadata["lens_model"] = _to_json_safe(exif_ifd.get(piexif.ExifIFD.LensModel))
        metadata["aperture"] = _format_aperture(exif_ifd.get(piexif.ExifIFD.FNumber))
        metadata["shutter_speed"] = _format_shutter(exif_ifd.get(piexif.ExifIFD.ExposureTime))
        metadata["exposure_compensation"] = _format_exposure_compensation(
            exif_ifd.get(piexif.ExifIFD.ExposureBiasValue)
        )
        metadata["iso"] = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
        metadata["focal_length"] = _format_focal_length(exif_ifd.get(piexif.ExifIFD.FocalLength))
        metadata["focal_length_35mm"] = _format_focal_length(
            exif_ifd.get(piexif.ExifIFD.FocalLengthIn35mmFilm)
        )
        metadata["metering_mode"] = exif_ifd.get(piexif.ExifIFD.MeteringMode)
        metadata["exposure_program"] = exif_ifd.get(piexif.ExifIFD.ExposureProgram)
        metadata["white_balance"] = exif_ifd.get(piexif.ExifIFD.WhiteBalance)
        metadata["flash"] = exif_ifd.get(piexif.ExifIFD.Flash)
        if metadata["metering_mode"] is not None:
            metadata["raw_exif"].setdefault("MeteringMode", _to_json_safe(metadata["metering_mode"]))
        if metadata["exposure_program"] is not None:
            metadata["raw_exif"].setdefault("ExposureProgram", _to_json_safe(metadata["exposure_program"]))
        if metadata["exposure_compensation"] is not None:
            metadata["raw_exif"].setdefault(
                "ExposureBiasValue",
                _to_json_safe(exif_ifd.get(piexif.ExifIFD.ExposureBiasValue)),
            )
        if metadata["white_balance"] is not None:
            metadata["raw_exif"].setdefault("WhiteBalance", _to_json_safe(metadata["white_balance"]))
        if metadata["flash"] is not None:
            metadata["raw_exif"].setdefault("Flash", _to_json_safe(metadata["flash"]))

        # 有些图没有色温
        color_temp = exif_ifd.get(piexif.ExifIFD.Temperature)
        metadata["color_temperature"] = str(color_temp) if color_temp is not None else None
        color_space = exif_ifd.get(piexif.ExifIFD.ColorSpace)
        if color_space is not None:
            metadata["color_space"] = _to_json_safe(color_space)

    if rating is None or rating_percent is None:
        xmp_rating, xmp_rating_percent = _extract_rating_from_file_xmp(image_path)
        if rating is None:
            rating = xmp_rating
        if rating_percent is None:
            rating_percent = xmp_rating_percent

    if rating is not None:
        metadata["raw_exif"]["Rating"] = rating
    if rating_percent is not None:
        metadata["raw_exif"]["RatingPercent"] = rating_percent

    return metadata
