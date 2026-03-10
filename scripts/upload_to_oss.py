from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote


TYPE_TO_PATH_FIELD = {
    "thumb": "thumb_path",
    "display": "display_path",
    "original": "original_path",
}

TYPE_TO_URL_FIELD = {
    "thumb": "thumb_url",
    "display": "display_url",
    "original": "original_url",
}

DATE_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_dotenv_file(path: Path, *, override: bool) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip("\"'")
        if not key:
            continue

        if override or key not in os.environ:
            os.environ[key] = value


def load_env(project_root: Path) -> None:
    load_dotenv_file(project_root / ".env_temp", override=False)
    load_dotenv_file(project_root / ".env", override=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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


def normalize_endpoint(raw_endpoint: str) -> str:
    endpoint = raw_endpoint.strip().rstrip("/")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"https://{endpoint}"


def build_public_base_url(endpoint: str, bucket_name: str) -> str:
    host = endpoint.split("://", 1)[-1].rstrip("/")
    return f"https://{bucket_name}.{host}"


def parse_date_dir(path_value: str, record: Dict[str, Any]) -> str:
    rel_path = Path(path_value)
    if len(rel_path.parts) >= 2:
        maybe_date = rel_path.parts[-2]
        if DATE_DIR_PATTERN.match(maybe_date):
            return maybe_date

    shot_time = str(record.get("shot_time") or "").strip()
    if len(shot_time) >= 10:
        maybe_date = shot_time[:10]
        if DATE_DIR_PATTERN.match(maybe_date):
            return maybe_date

    return "unknown-date"


def build_object_key(image_type: str, path_value: str, record: Dict[str, Any]) -> str:
    filename = Path(path_value).name
    if not filename:
        raise ValueError(f"Invalid image path for type={image_type}: {path_value}")

    date_dir = parse_date_dir(path_value, record)
    return f"{image_type}/{date_dir}/{filename}"


def resolve_local_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def create_bucket_client(access_key_id: str, access_key_secret: str, endpoint: str, bucket_name: str):
    try:
        import oss2  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: oss2. Install with `pip install oss2`.") from exc

    auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name)


def upload_one_file(
    bucket: Any,
    local_path: Path,
    object_key: str,
    public_base_url: str,
) -> str:
    result = bucket.put_object_from_file(object_key, str(local_path))
    if getattr(result, "status", None) not in {200, 201, 204}:
        raise RuntimeError(f"OSS upload failed: key={object_key}, status={getattr(result, 'status', None)}")

    encoded_key = quote(object_key, safe="/-_.~")
    return f"{public_base_url.rstrip('/')}/{encoded_key}"


def process_records(
    records: List[Dict[str, Any]],
    project_root: Path,
    bucket: Any,
    public_base_url: str,
    skip_existing_urls: bool,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    output_records: List[Dict[str, Any]] = []
    failed_records: List[Dict[str, Any]] = []

    for idx, record in enumerate(records, start=1):
        uuid = record.get("uuid", f"unknown_{idx}")
        updated = dict(record)

        try:
            for image_type, path_field in TYPE_TO_PATH_FIELD.items():
                url_field = TYPE_TO_URL_FIELD[image_type]

                if skip_existing_urls and str(updated.get(url_field) or "").strip():
                    continue

                path_value = str(updated.get(path_field) or "").strip()
                if not path_value:
                    raise ValueError(f"record missing required field: {path_field}")

                local_path = resolve_local_path(project_root, path_value)
                if not local_path.exists():
                    raise FileNotFoundError(f"local file not found: {local_path}")

                object_key = build_object_key(image_type, path_value, updated)
                uploaded_url = upload_one_file(
                    bucket=bucket,
                    local_path=local_path,
                    object_key=object_key,
                    public_base_url=public_base_url,
                )
                updated[url_field] = uploaded_url

            updated.pop("upload_error", None)
            logging.info("[%d/%d] Uploaded uuid=%s", idx, len(records), uuid)

        except Exception as exc:
            error_text = str(exc)
            updated["upload_error"] = error_text
            failed_records.append(
                {
                    "uuid": uuid,
                    "error": error_text,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "record": record,
                }
            )
            logging.exception("[%d/%d] Upload failed uuid=%s", idx, len(records), uuid)

        output_records.append(updated)

    return output_records, failed_records


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload photo files to Aliyun OSS and write URL fields to a new JSONL.")
    parser.add_argument("--input-jsonl", default="output/metadata/photos_ai.jsonl", help="Input JSONL path.")
    parser.add_argument(
        "--output-jsonl",
        default="output/metadata/photos_ai_oss.jsonl",
        help="Output JSONL path. Original input file will not be modified.",
    )
    parser.add_argument(
        "--failed-jsonl",
        default="output/metadata/photos_ai_oss_failed.jsonl",
        help="Failed upload records JSONL path.",
    )
    parser.add_argument(
        "--skip-existing-urls",
        action="store_true",
        help="Skip upload when thumb_url/display_url/original_url already exists in a record.",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level, e.g. INFO/DEBUG/WARNING.")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    setup_logging(args.log_level)

    project_root = Path(__file__).resolve().parent.parent
    load_env(project_root)

    access_key_id = require_env("OSS_ACCESS_KEY_ID")
    access_key_secret = require_env("OSS_ACCESS_KEY_SECRET")
    bucket_name = require_env("OSS_BUCKET_NAME")
    endpoint_raw = require_env("OSS_ENDPOINT")

    endpoint = normalize_endpoint(endpoint_raw)
    public_base_url = os.getenv("OSS_PUBLIC_BASE_URL", "").strip() or build_public_base_url(endpoint, bucket_name)

    input_jsonl = (project_root / args.input_jsonl).resolve()
    output_jsonl = (project_root / args.output_jsonl).resolve()
    failed_jsonl = (project_root / args.failed_jsonl).resolve()

    logging.info("Input JSONL: %s", input_jsonl)
    logging.info("Output JSONL: %s", output_jsonl)
    logging.info("Failed JSONL: %s", failed_jsonl)
    logging.info("Bucket: %s", bucket_name)
    logging.info("Endpoint: %s", endpoint)

    bucket = create_bucket_client(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=endpoint,
        bucket_name=bucket_name,
    )

    records = read_jsonl(input_jsonl)
    output_records, failed_records = process_records(
        records=records,
        project_root=project_root,
        bucket=bucket,
        public_base_url=public_base_url,
        skip_existing_urls=bool(args.skip_existing_urls),
    )

    write_jsonl(output_jsonl, output_records)
    write_jsonl(failed_jsonl, failed_records)

    logging.info(
        "Done. Total=%d, Success=%d, Failed=%d",
        len(records),
        len(records) - len(failed_records),
        len(failed_records),
    )


if __name__ == "__main__":
    main()
