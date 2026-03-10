from __future__ import annotations

import argparse
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml


TAG_TYPE_TO_FIELD = {
    "subject": "subject_tags",
    "element": "element_tags",
    "mood": "mood_tags",
}

NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
UUID_FALLBACK_NAMESPACE = uuid.UUID("d5f69b79-58ad-4fcb-a268-9e0336d90f3d")


def load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"JSONL file not found: {path}")

    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            row = line.strip()
            if not row:
                continue
            try:
                records.append(json.loads(row))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc
    return records


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_record_uuid(value: Any) -> Tuple[Optional[str], Optional[str]]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        normalized_uuid = str(uuid.UUID(text))
        return normalized_uuid, text
    except Exception:
        # Input JSONL uses non-RFC UUID ids (e.g. 20241231_234434_f967).
        # Generate a stable UUIDv5 so photos.uuid can always be populated.
        generated_uuid = str(uuid.uuid5(UUID_FALLBACK_NAMESPACE, text))
        return generated_uuid, text


def parse_optional_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except Exception:
        return None


def parse_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None

    match = NUMBER_PATTERN.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def normalize_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_tag_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []

    output: List[str] = []
    seen = set()
    for item in value:
        tag = str(item).strip()
        if not tag:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        output.append(tag)
    return output


def build_photo_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    db_uuid, source_uuid = parse_record_uuid(record.get("uuid"))

    payload: Dict[str, Any] = {
        "uuid": db_uuid,
        "filename": normalize_string(record.get("filename")),
        "title_cn": normalize_string(record.get("title_cn")),
        "title_en": normalize_string(record.get("title_en")),
        "description": normalize_string(record.get("description")),
        "category": normalize_string(record.get("category")),
        "shot_time": parse_optional_datetime(record.get("shot_time")),
        "year": parse_optional_int(record.get("year")),
        "month": parse_optional_int(record.get("month")),
        "day": parse_optional_int(record.get("day")),
        "hour": parse_optional_int(record.get("hour")),
        "minute": parse_optional_int(record.get("minute")),
        "second": parse_optional_int(record.get("second")),
        "width": parse_optional_int(record.get("width")),
        "height": parse_optional_int(record.get("height")),
        "orientation": normalize_string(record.get("orientation")),
        "resolution": normalize_string(record.get("resolution")),
        "camera_model": normalize_string(record.get("camera_model")),
        "lens_model": normalize_string(record.get("lens_model")),
        "aperture": normalize_string(record.get("aperture")),
        "shutter_speed": normalize_string(record.get("shutter_speed")),
        "exposure_compensation": normalize_string(record.get("exposure_compensation")),
        "iso": parse_optional_int(record.get("iso")),
        "focal_length": parse_optional_float(record.get("focal_length")),
        "focal_length_35mm": parse_optional_float(record.get("focal_length_35mm")),
        "metering_mode": normalize_string(record.get("metering_mode")),
        "exposure_program": normalize_string(record.get("exposure_program")),
        "white_balance": normalize_string(record.get("white_balance")),
        "flash": normalize_string(record.get("flash")),
        "author": normalize_string(record.get("author")),
        "raw_exif": parse_json_object(record.get("raw_exif")),
        "ai_metadata": parse_json_object(record.get("ai_metadata")),
        "extra_metadata": parse_json_object(record.get("extra_metadata")),
        "thumb_url": normalize_string(record.get("thumb_url")),
        "display_url": normalize_string(record.get("display_url")),
        "original_url": normalize_string(record.get("original_url")),
        "is_published": bool(record.get("is_published", True)),
    }

    if source_uuid and source_uuid != db_uuid:
        payload["extra_metadata"] = dict(payload["extra_metadata"])
        payload["extra_metadata"]["source_uuid"] = source_uuid

    if not payload["filename"]:
        raise ValueError("record missing filename")

    return payload


PHOTO_COLUMNS: Sequence[str] = (
    "uuid", "filename", "title_cn", "title_en", "description", "category", "shot_time",
    "year", "month", "day", "hour", "minute", "second", "width", "height", "orientation",
    "resolution", "camera_model", "lens_model", "aperture", "shutter_speed", "iso",
    "exposure_compensation", "focal_length", "focal_length_35mm", "metering_mode", "exposure_program", "white_balance",
    "flash", "author", "raw_exif", "ai_metadata", "extra_metadata", "thumb_url", "display_url",
    "original_url", "is_published",
)

PHOTO_UPDATE_COLUMNS: Sequence[str] = (
    "uuid", "filename", "title_cn", "title_en", "description", "category", "shot_time",
    "year", "month", "day", "hour", "minute", "second", "width", "height", "orientation",
    "resolution", "camera_model", "lens_model", "aperture", "shutter_speed", "iso",
    "exposure_compensation", "focal_length", "focal_length_35mm", "metering_mode", "exposure_program", "white_balance",
    "flash", "author", "raw_exif", "ai_metadata", "extra_metadata", "thumb_url", "display_url",
    "original_url", "is_published",
)


def ensure_tag(cur: Any, name: str, tag_type: str) -> int:
    cur.execute(
        """
        INSERT INTO tags (name, tag_type)
        VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING
        """,
        (name, tag_type),
    )
    cur.execute("SELECT id, tag_type FROM tags WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Failed to get tag id for name={name}")
    tag_id, existing_type = row
    if existing_type != tag_type:
        logging.warning(
            "Tag type mismatch for name=%s. Existing=%s, incoming=%s. Keep existing type.",
            name,
            existing_type,
            tag_type,
        )
    return int(tag_id)


def replace_photo_tags(cur: Any, photo_id: int, tag_ids: Sequence[int]) -> None:
    cur.execute("DELETE FROM photo_tags WHERE photo_id = %s", (photo_id,))
    for tag_id in tag_ids:
        cur.execute(
            """
            INSERT INTO photo_tags (photo_id, tag_id)
            VALUES (%s, %s)
            ON CONFLICT (photo_id, tag_id) DO NOTHING
            """,
            (photo_id, tag_id),
        )


def find_existing_photo_id(cur: Any, payload: Dict[str, Any]) -> Optional[int]:
    if payload.get("original_url"):
        cur.execute("SELECT id FROM photos WHERE original_url = %s LIMIT 1", (payload["original_url"],))
        row = cur.fetchone()
        if row:
            return int(row[0])

    if payload.get("filename") and payload.get("shot_time"):
        cur.execute(
            "SELECT id FROM photos WHERE filename = %s AND shot_time = %s LIMIT 1",
            (payload["filename"], payload["shot_time"]),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])

    return None


def find_photo_id_by_uuid(cur: Any, photo_uuid: Optional[str]) -> Optional[int]:
    if not photo_uuid:
        return None
    cur.execute("SELECT id FROM photos WHERE uuid = %s LIMIT 1", (photo_uuid,))
    row = cur.fetchone()
    if row:
        return int(row[0])
    return None


def insert_photo(cur: Any, payload: Dict[str, Any], json_adapter: Any) -> int:
    values = []
    for col in PHOTO_COLUMNS:
        value = payload[col]
        if col in {"raw_exif", "ai_metadata", "extra_metadata"}:
            value = json_adapter(value)
        values.append(value)

    placeholders = ", ".join(["%s"] * len(PHOTO_COLUMNS))
    sql = f"""
        INSERT INTO photos ({", ".join(PHOTO_COLUMNS)})
        VALUES ({placeholders})
        RETURNING id
    """
    cur.execute(sql, values)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Failed to insert photo")
    return int(row[0])


def update_photo_by_id(cur: Any, photo_id: int, payload: Dict[str, Any], json_adapter: Any) -> int:
    set_parts = []
    values = []
    for col in PHOTO_UPDATE_COLUMNS:
        set_parts.append(f"{col} = %s")
        value = payload[col]
        if col in {"raw_exif", "ai_metadata", "extra_metadata"}:
            value = json_adapter(value)
        values.append(value)
    values.append(photo_id)

    sql = f"""
        UPDATE photos
        SET {", ".join(set_parts)}
        WHERE id = %s
        RETURNING id
    """
    cur.execute(sql, values)
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Failed to update photo id={photo_id}")
    return int(row[0])


def import_single_record(cur: Any, record: Dict[str, Any], json_adapter: Any) -> int:
    payload = build_photo_payload(record)

    existing_id = find_photo_id_by_uuid(cur, payload["uuid"])
    if existing_id is None:
        existing_id = find_existing_photo_id(cur, payload)

    if existing_id is None:
        photo_id = insert_photo(cur, payload, json_adapter)
    else:
        photo_id = update_photo_by_id(cur, existing_id, payload, json_adapter)

    tag_ids: List[int] = []
    for tag_type, field_name in TAG_TYPE_TO_FIELD.items():
        for tag_name in normalize_tag_list(record.get(field_name)):
            tag_id = ensure_tag(cur, tag_name, tag_type)
            tag_ids.append(tag_id)

    unique_tag_ids = list(dict.fromkeys(tag_ids))
    replace_photo_tags(cur, photo_id, unique_tag_ids)
    return photo_id


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import photos_ai_oss.jsonl into PostgreSQL tables.")
    parser.add_argument("--input-jsonl", default="output/metadata/photos_ai_oss.jsonl", help="Input JSONL path.")
    parser.add_argument(
        "--failed-jsonl",
        default="output/metadata/import_pg_failed.jsonl",
        help="Path to write failed records.",
    )
    parser.add_argument("--db-host", default=None, help="PostgreSQL host. Default from settings.yaml pg_host.")
    parser.add_argument("--db-port", type=int, default=None, help="PostgreSQL port. Default from settings.yaml pg_port.")
    parser.add_argument("--db-name", default=None, help="PostgreSQL database. Default from settings.yaml pg_database.")
    parser.add_argument("--db-user", default=None, help="PostgreSQL user. Default from settings.yaml pg_user.")
    parser.add_argument("--db-password", default=None, help="PostgreSQL password. Default from settings.yaml pg_password.")
    parser.add_argument("--log-level", default="INFO", help="Logging level, e.g. INFO/DEBUG/WARNING.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop immediately when one record fails.")
    return parser


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config = load_config(project_root / "config" / "settings.yaml")

    parser = build_arg_parser()
    args = parser.parse_args()
    setup_logging(args.log_level)

    db_host = args.db_host or str(config["pg_host"])
    db_port = args.db_port if args.db_port is not None else int(config["pg_port"])
    db_name = args.db_name or str(config["pg_database"])
    db_user = args.db_user or str(config["pg_user"])
    db_password = args.db_password or str(config["pg_password"])
    db_timezone = str(config.get("pg_timezone", "Asia/Shanghai"))

    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import Json  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: psycopg2. Install with `pip install psycopg2-binary`.") from exc

    input_jsonl = (project_root / args.input_jsonl).resolve()
    failed_jsonl = (project_root / args.failed_jsonl).resolve()
    records = read_jsonl(input_jsonl)

    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        dbname=db_name,
        user=db_user,
        password=db_password,
        connect_timeout=10,
    )
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE %s", (db_timezone,))
    conn.commit()
    logging.info("PostgreSQL session timezone: %s", db_timezone)

    success_count = 0
    fail_count = 0
    failed_records: List[Dict[str, Any]] = []

    try:
        for idx, record in enumerate(records, start=1):
            source_uuid = str(record.get("uuid", f"unknown_{idx}"))
            try:
                with conn:
                    with conn.cursor() as cur:
                        photo_id = import_single_record(cur, record, Json)
                success_count += 1
                logging.info("[%d/%d] Imported uuid=%s -> photo_id=%s", idx, len(records), source_uuid, photo_id)
            except Exception as exc:
                fail_count += 1
                logging.exception("[%d/%d] Import failed for uuid=%s", idx, len(records), source_uuid)
                failed_records.append(
                    {
                        "uuid": source_uuid,
                        "error": str(exc),
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "record": record,
                    }
                )
                if args.stop_on_error:
                    break
    finally:
        conn.close()

    write_jsonl(failed_jsonl, failed_records)
    logging.info("Done. Total=%d, Success=%d, Failed=%d", len(records), success_count, fail_count)
    logging.info("Failed record file: %s", failed_jsonl)


if __name__ == "__main__":
    main()
