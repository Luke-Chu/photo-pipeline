from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml
import torch
from transformers import Qwen3VLForConditionalGeneration, AutoProcessor


VALID_CATEGORIES = {
    "风光", "城市", "街拍", "建筑", "人文", "旅行", "自然", "夜景", "纪实"
}


def load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "generate_ai_metadata.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_prompt(prompt_path: Path) -> str:
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"JSONL file not found: {path}")

    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSONL at line {line_no}: {e}") from e
    return records


def write_jsonl(records: List[Dict[str, Any]], path: Path) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(record: Dict[str, Any], path: Path) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_processed_uuid_set(output_jsonl: Path) -> Set[str]:
    processed: Set[str] = set()
    if not output_jsonl.exists():
        return processed

    with open(output_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                uuid = item.get("uuid")
                if uuid:
                    processed.add(uuid)
            except Exception:
                continue
    return processed


def extract_first_json_block(text: str) -> Dict[str, Any]:
    """
    尽量从模型输出中提取第一个 JSON 对象
    """
    text = text.strip()

    # 去掉 markdown code fence
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # 先尝试整体解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 再尝试抓取第一个大括号对象
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"Failed to parse JSON from model output: {text}")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def clean_tag_list(value: Any, min_count: int, max_count: int) -> List[str]:
    if not isinstance(value, list):
        return []

    cleaned: List[str] = []
    seen = set()
    for item in value:
        tag = clean_text(item)
        if not tag:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)

    return cleaned[:max_count] if len(cleaned) >= min_count else cleaned


def normalize_ai_result(raw_result: Dict[str, Any]) -> Dict[str, Any]:
    title_cn = clean_text(raw_result.get("title_cn"))
    description = clean_text(raw_result.get("description"))
    category = clean_text(raw_result.get("category"))

    if category not in VALID_CATEGORIES:
        # 简单兜底
        if "夜" in title_cn or "夜" in description:
            category = "夜景"
        elif "建筑" in description:
            category = "建筑"
        elif "城市" in description:
            category = "城市"
        else:
            category = "纪实"

    subject_tags = clean_tag_list(raw_result.get("subject_tags"), min_count=1, max_count=4)
    element_tags = clean_tag_list(raw_result.get("element_tags"), min_count=1, max_count=8)
    mood_tags = clean_tag_list(raw_result.get("mood_tags"), min_count=1, max_count=5)

    if not title_cn:
        title_cn = "未命名作品"

    if not description:
        description = "暂无描述。"

    return {
        "title_cn": title_cn[:40],
        "description": description[:200],
        "category": category,
        "subject_tags": subject_tags,
        "element_tags": element_tags,
        "mood_tags": mood_tags,
    }


def build_image_path(project_root: Path, record: Dict[str, Any], image_source: str) -> Path:
    source = str(image_source).strip().lower()

    if source == "display":
        rel = record.get("display_path")
    elif source == "original":
        rel = record.get("original_path")
    elif source in {"thumb", "thumbs", "thumbnail"}:
        rel = record.get("thumb_path")
    else:
        raise ValueError(
            "Unsupported ai_image_source: "
            f"{image_source}. Supported values: display, original, thumb/thumbs/thumbnail"
        )

    if not rel:
        raise ValueError(f"Record {record.get('uuid')} missing image path for source={image_source}")

    return project_root / rel


def load_model_and_processor(model_path: str, processor_path: str):
    logging.info("Loading model from: %s", model_path)
    logging.info("Loading processor from: %s", processor_path)

    model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_path,
        dtype="auto",
        device_map="auto",
    )
    processor = AutoProcessor.from_pretrained(processor_path)
    return model, processor


def generate_single_result(
    model,
    processor,
    image_path: Path,
    prompt_text: str,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    do_sample: bool,
) -> Dict[str, Any]:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "image": str(image_path.resolve()),
                },
                {
                    "type": "text",
                    "text": prompt_text,
                },
            ],
        }
    ]

    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )

    inputs = inputs.to(model.device)

    with torch.no_grad():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            temperature=temperature if do_sample else None,
            top_p=top_p if do_sample else None,
        )

    generated_ids_trimmed = [
        out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]

    output_text = processor.batch_decode(
        generated_ids_trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]

    parsed = extract_first_json_block(output_text)
    normalized = normalize_ai_result(parsed)

    return {
        "normalized": normalized,
        "raw_output_text": output_text,
        "parsed_raw_json": parsed,
    }


def merge_record_with_ai(
    record: Dict[str, Any],
    ai_result: Dict[str, Any],
    model_name: str,
    prompt_version: str,
) -> Dict[str, Any]:
    merged = dict(record)

    normalized = ai_result["normalized"]

    merged["title_cn"] = normalized["title_cn"]
    merged["description"] = normalized["description"]
    merged["category"] = normalized["category"]
    merged["subject_tags"] = normalized["subject_tags"]
    merged["element_tags"] = normalized["element_tags"]
    merged["mood_tags"] = normalized["mood_tags"]

    merged["ai_metadata"] = {
        "model": model_name,
        "prompt_version": prompt_version,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "raw_output_text": ai_result["raw_output_text"],
        "parsed_raw_json": ai_result["parsed_raw_json"],
    }

    return merged


def should_skip_record(record: Dict[str, Any], skip_completed: bool) -> bool:
    if not skip_completed:
        return False
    title_cn = record.get("title_cn")
    return bool(title_cn and str(title_cn).strip())


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config = load_config(project_root / "config" / "settings.yaml")

    setup_logging(project_root / config["output_logs_dir"])

    prompt_path = project_root / config["ai_prompt_file"]
    input_jsonl = project_root / config["ai_input_jsonl"]
    output_jsonl = project_root / config["ai_output_jsonl"]
    failed_jsonl = project_root / config["ai_failed_jsonl"]

    qwen_model_path = config["qwen_model_path"]
    qwen_processor_path = config["qwen_processor_path"]
    image_source = config["ai_image_source"]
    skip_completed = bool(config["ai_skip_completed"])
    resume = bool(config["ai_resume"])

    max_new_tokens = int(config["ai_max_new_tokens"])
    temperature = float(config["ai_temperature"])
    top_p = float(config["ai_top_p"])
    do_sample = bool(config["ai_do_sample"])
    prompt_version = str(config["ai_prompt_version"])

    prompt_text = load_prompt(prompt_path)
    records = read_jsonl(input_jsonl)

    processed_uuids: Set[str] = set()
    if resume:
        processed_uuids = build_processed_uuid_set(output_jsonl)
        logging.info("Resume mode on. Already processed: %d", len(processed_uuids))

    model, processor = load_model_and_processor(qwen_model_path, qwen_processor_path)

    model_name_for_metadata = Path(qwen_model_path).name

    success_count = 0
    skip_count = 0
    fail_count = 0

    for idx, record in enumerate(records, start=1):
        uuid = record.get("uuid", f"unknown_{idx}")

        if resume and uuid in processed_uuids:
            logging.info("[%d/%d] Skip already processed uuid=%s", idx, len(records), uuid)
            skip_count += 1
            continue

        if should_skip_record(record, skip_completed):
            logging.info("[%d/%d] Skip completed record uuid=%s", idx, len(records), uuid)
            append_jsonl(record, output_jsonl)
            success_count += 1
            continue

        try:
            image_path = build_image_path(project_root, record, image_source)
            if not image_path.exists():
                raise FileNotFoundError(f"Image not found: {image_path}")

            logging.info("[%d/%d] Generating AI metadata for uuid=%s, image=%s",
                         idx, len(records), uuid, image_path.name)

            ai_result = generate_single_result(
                model=model,
                processor=processor,
                image_path=image_path,
                prompt_text=prompt_text,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                do_sample=do_sample,
            )

            merged_record = merge_record_with_ai(
                record=record,
                ai_result=ai_result,
                model_name=model_name_for_metadata,
                prompt_version=prompt_version,
            )

            append_jsonl(merged_record, output_jsonl)
            success_count += 1

            logging.info(
                "Done uuid=%s | title=%s | category=%s",
                uuid,
                merged_record.get("title_cn"),
                merged_record.get("category"),
            )

        except Exception as e:
            fail_count += 1
            logging.exception("Failed on uuid=%s", uuid)

            failed_record = {
                "uuid": uuid,
                "error": str(e),
                "record": record,
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            }
            append_jsonl(failed_record, failed_jsonl)

    logging.info(
        "Finished. Success=%d, Skip=%d, Failed=%d, Output=%s",
        success_count, skip_count, fail_count, output_jsonl
    )


if __name__ == "__main__":
    main()
