from __future__ import annotations

import csv
import json
import logging
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml
from PIL import Image, ImageOps

from extract_exif import extract_image_metadata


def make_json_safe(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            return str(value)
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        try:
            if value.denominator == 0:
                return None
            return round(float(value.numerator) / float(value.denominator), 6)
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple)):
        return [make_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    return str(value)
    

def load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "process.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def ensure_dirs(paths: List[Path]) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def iter_images(input_dir: Path, extensions: List[str]) -> List[Path]:
    exts = {e.lower() for e in extensions}
    files = [p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    files.sort()
    return files


def year_month_day_from_shot_time(shot_time: str | None) -> str:
    """
    shot_time 预期格式：2024-10-15 18:42:10
    输出：2024/10
    """
    if shot_time and len(shot_time) >= 7:
        year = shot_time[:4]
        month = shot_time[5:7]
        day = shot_time[8:10]
        if year.isdigit() and month.isdigit() and day.isdigit():
            return f"{year}-{month}-{day}"
    return "unknown/unknown"


def generate_resized_image(
    input_path: Path,
    output_path: Path,
    max_size: int,
    quality: int,
    overwrite: bool,
) -> None:
    if output_path.exists() and not overwrite:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(input_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        img.thumbnail((max_size, max_size))
        img.save(output_path, format="JPEG", quality=quality, optimize=True)


def write_jsonl(records: List[Dict[str, Any]], output_path: Path) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            safe_record = make_json_safe(record)
            f.write(json.dumps(safe_record, ensure_ascii=False) + "\n")


def write_csv(records: List[Dict[str, Any]], output_path: Path) -> None:
    if not records:
        return

    def flatten(record: Dict[str, Any]) -> Dict[str, Any]:
        flat = dict(record)
        # raw_exif 过大，CSV 用字符串保留
        flat["raw_exif"] = json.dumps(make_json_safe(record.get("raw_exif", {})), ensure_ascii=False)
        return flat

    flat_records = [flatten(r) for r in records]
    fieldnames = list(flat_records[0].keys())

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_records)


def short_hash(text):
    return hashlib.md5(text.encode()).hexdigest()[:4]


def is_normalized_filename(filename: str) -> bool:
    return re.fullmatch(r"(?:\d{8}_\d{6}|unknown_time)_[0-9a-f]{4}\.jpg", filename.lower()) is not None


def build_new_filename(metadata, image_path):
    """
    生成统一文件名
    格式：YYYYMMDD_HHMMSS_原文件名.jpg
    """
    if is_normalized_filename(image_path.name):
        return image_path.stem, image_path.name

    shot_time = metadata.get("shot_time")

    if shot_time:
        # 2024-10-15 18:42:10
        dt = shot_time.replace("-", "").replace(":", "").replace(" ", "_")
        dt = dt[:15]  # 20241015_184210
    else:
        dt = "unknown_time"

    hash_part = short_hash(image_path.name)
    uuid = f"{dt}_{hash_part}"

    return f"{uuid}", f"{uuid}.jpg"


def rename_original_image(image_path: Path, new_filename: str, overwrite: bool) -> Path:
    renamed_path = image_path.with_name(new_filename)
    if renamed_path == image_path:
        return image_path

    if renamed_path.exists():
        if not overwrite:
            raise FileExistsError(f"Target original filename already exists: {renamed_path}")
        if renamed_path.is_dir():
            raise IsADirectoryError(f"Target path is a directory: {renamed_path}")
        renamed_path.unlink()

    image_path.rename(renamed_path)
    return renamed_path


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config = load_config(project_root / "config" / "settings.yaml")

    input_dir = project_root / config["input_dir"]
    output_display_dir = project_root / config["output_display_dir"]
    output_thumb_dir = project_root / config["output_thumb_dir"]
    output_metadata_dir = project_root / config["output_metadata_dir"]
    output_logs_dir = project_root / config["output_logs_dir"]

    thumb_max_size = int(config["thumb_max_size"])
    display_max_size = int(config["display_max_size"])
    thumb_quality = int(config["thumb_quality"])
    display_quality = int(config["display_quality"])
    supported_extensions = config["supported_extensions"]
    default_author = config["default_author"]
    overwrite = bool(config["overwrite"])

    ensure_dirs([
        input_dir,
        output_display_dir,
        output_thumb_dir,
        output_metadata_dir,
        output_logs_dir,
    ])
    setup_logging(output_logs_dir)

    image_files = iter_images(input_dir, supported_extensions)
    logging.info("Found %d image(s).", len(image_files))

    records: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []

    for idx, image_path in enumerate(image_files, start=1):
        try:
            logging.info("[%d/%d] Processing %s", idx, len(image_files), image_path.name)

            metadata = extract_image_metadata(image_path, default_author=default_author)
            
            uuid, new_filename = build_new_filename(metadata, image_path)
            year_month_day = year_month_day_from_shot_time(metadata.get("shot_time"))
            output_display_path = output_display_dir / year_month_day / new_filename
            output_thumb_path = output_thumb_dir / year_month_day / new_filename

            generate_resized_image(
                image_path,
                output_display_path,
                display_max_size,
                display_quality,
                overwrite,
            )
            generate_resized_image(
                image_path,
                output_thumb_path,
                thumb_max_size,
                thumb_quality,
                overwrite,
            )
            renamed_original_path = rename_original_image(image_path, new_filename, overwrite)
            if renamed_original_path != image_path:
                logging.info("Renamed original: %s -> %s", image_path.name, renamed_original_path.name)

            record = {
                "uuid": uuid,
                "original_filename": renamed_original_path.name,
                "new_filename": new_filename,
                "title_cn": None,
                "title_en": None,
                "description": None,
                "category": None,
                "subject_tags": [],
                "element_tags": [],
                "mood_tags": [],
                "shot_time": metadata["shot_time"],
                "year": int(metadata["shot_time"][:4]) if metadata["shot_time"] else None,
                "month": int(metadata["shot_time"][5:7]) if metadata["shot_time"] else None,
                "day": int(metadata["shot_time"][8:10]) if metadata["shot_time"] else None,
                "width": metadata["width"],
                "height": metadata["height"],
                "resolution": metadata["resolution"],
                "camera_model": metadata["camera_model"],
                "lens_model": metadata["lens_model"],
                "aperture": metadata["aperture"],
                "shutter_speed": metadata["shutter_speed"],
                "iso": metadata["iso"],
                "focal_length": metadata["focal_length"],
                "focal_length_35mm": metadata["focal_length_35mm"],
                "metering_mode": metadata["metering_mode"],
                "exposure_program": metadata["exposure_program"],
                "white_balance": metadata["white_balance"],
                "color_temperature": metadata["color_temperature"],
                "color_space": metadata["color_space"],
                "bit_depth": metadata["bit_depth"],
                "author": metadata["author"],
                "thumb_path": str(output_thumb_path.relative_to(project_root)).replace("\\", "/"),
                "display_path": str(output_display_path.relative_to(project_root)).replace("\\", "/"),
                "original_path": str(renamed_original_path.relative_to(project_root)).replace("\\", "/"),
                "raw_exif": metadata["raw_exif"],
                "ai_metadata": {},
                "extra_metadata": {},
            }

            records.append(record)

        except Exception as e:
            logging.exception("Failed to process %s", image_path)
            failed.append({
                "file": str(image_path),
                "error": str(e),
            })

    jsonl_path = output_metadata_dir / "photos.jsonl"
    csv_path = output_metadata_dir / "photos.csv"
    failed_path = output_metadata_dir / "failed.jsonl"

    write_jsonl(records, jsonl_path)
    write_csv(records, csv_path)
    write_jsonl(failed, failed_path)

    logging.info("Done. Success: %d, Failed: %d", len(records), len(failed))
    logging.info("JSONL written to: %s", jsonl_path)
    logging.info("CSV written to: %s", csv_path)
    logging.info("Failed log written to: %s", failed_path)


if __name__ == "__main__":
    main()
