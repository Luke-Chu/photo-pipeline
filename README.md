# Photo Pipeline

一个面向个人摄影作品库的数据处理流水线，覆盖从原图整理、EXIF 提取、AI 文案生成、OSS 上传到 PostgreSQL 入库的完整流程。

## 功能概览

- 批量扫描原图并生成统一命名
- 提取 EXIF 元数据并标准化
- 生成 `display`/`thumb` 两种派生图
- 产出结构化元数据 `JSONL/CSV`
- 基于 Qwen3-VL 自动生成标题、描述、分类和标签
- 上传三种图片（`thumb/display/original`）到阿里云 OSS 并回填 URL
- 将最终数据导入 PostgreSQL（含 `photos/tags/photo_tags`）

## 项目结构

```text
photo-pipeline/
├─ config/
│  └─ settings.yaml
├─ prompts/
│  └─ photo_metadata_prompt.txt
├─ scripts/
│  ├─ extract_exif.py
│  ├─ process_images.py
│  ├─ generate_ai_metadata.py
│  ├─ upload_to_oss.py
│  └─ import_pg.py
└─ .env.example
```

运行后会生成（默认）：

```text
input/originals/                 # 原图输入目录（脚本会重命名原图）
output/display/YYYY-MM-DD/*.jpg  # 展示图
output/thumbs/YYYY-MM-DD/*.jpg   # 缩略图
output/metadata/*.jsonl|*.csv    # 各阶段元数据
output/logs/*.log                # 日志
```

## 流水线顺序

1. `process_images.py`
2. `generate_ai_metadata.py`
3. `upload_to_oss.py`
4. `import_pg.py`

建议严格按顺序执行。

## 环境要求

- Python 3.10+（建议 3.11/3.12）
- PostgreSQL 14+
- 阿里云 OSS Bucket（外网可访问）
- 若执行 AI 阶段：
  - `torch` + `transformers`
  - 本地 Qwen3-VL 模型目录可用
  - 建议有可用 GPU（CPU 也可但较慢）

## 安装依赖

项目当前未提供 `requirements.txt`，可按脚本依赖手动安装：

```bash
pip install pyyaml pillow piexif oss2 psycopg2-binary
pip install torch transformers
```

## 配置说明

### 1) `config/settings.yaml`

核心配置全部在该文件中，包括图片处理参数、AI 参数、模型路径、数据库连接。

关键字段：

- `input_dir`：原图目录
- `output_display_dir` / `output_thumb_dir` / `output_metadata_dir` / `output_logs_dir`
- `thumb_max_size` / `display_max_size`
- `thumb_quality` / `display_quality`
- `supported_extensions`
- `default_author`
- `overwrite`
- `exif_term_mappings.*`
- `qwen_model_path` / `qwen_processor_path`
- `ai_prompt_file` / `ai_input_jsonl` / `ai_output_jsonl` / `ai_failed_jsonl`
- `ai_image_source`（`thumb`/`display`/`original`）
- `ai_skip_completed` / `ai_resume`
- `ai_max_new_tokens` / `ai_temperature` / `ai_top_p` / `ai_do_sample` / `ai_repetition_penalty`
- `ai_prompt_version`
- `pg_host` / `pg_port` / `pg_database` / `pg_user` / `pg_password` / `pg_timezone`

### 2) `.env`（仅 OSS）

`upload_to_oss.py` 从 `.env` 读取 OSS 凭证，支持变量如下（见 `.env.example`）：

- `OSS_ACCESS_KEY_ID`
- `OSS_ACCESS_KEY_SECRET`
- `OSS_BUCKET_NAME`
- `OSS_ENDPOINT`
- `OSS_PUBLIC_BASE_URL`（可选，不填则自动拼接）

## 数据库结构要求

`import_pg.py` 依赖以下表：

- `photos`
- `tags`
- `photo_tags`

关键约束建议：

- `photos.id` 主键
- `photos.uuid` 唯一（脚本会将非标准 uuid 稳定映射为 UUIDv5）
- `tags.name` 唯一
- `photo_tags(photo_id, tag_id)` 联合主键

`import_pg.py` 会：

- upsert/更新 `photos`（按 `uuid`，或回退 `original_url`、`filename+shot_time` 识别）
- 按标签名写入 `tags`
- 重建单图关联的 `photo_tags`
- 会话级设置 `SET TIME ZONE` 为 `settings.yaml` 的 `pg_timezone`

## 执行步骤

### 步骤 1：处理原图并抽取元数据

```bash
python scripts/process_images.py
```

输出：

- `output/metadata/photos.jsonl`
- `output/metadata/photos.csv`
- `output/metadata/failed.jsonl`

注意：

- 原图会在 `input/originals` 内被重命名为统一格式（如 `YYYYMMDD_HHMMSS_abcd.jpg`）。

### 步骤 2：生成 AI 文案与标签

```bash
python scripts/generate_ai_metadata.py
```

输入：`settings.yaml` 中 `ai_input_jsonl`（默认 `output/metadata/photos.jsonl`）  
输出：`ai_output_jsonl`（默认 `output/metadata/photos_ai.jsonl`）和失败文件。

### 步骤 3：上传到 OSS 并回填 URL

```bash
python scripts/upload_to_oss.py
```

常用参数：

```bash
python scripts/upload_to_oss.py \
  --input-jsonl output/metadata/photos_ai.jsonl \
  --output-jsonl output/metadata/photos_ai_oss.jsonl \
  --failed-jsonl output/metadata/photos_ai_oss_failed.jsonl \
  --skip-existing-urls
```

对象路径格式：

- `thumb/YYYY-MM-DD/<filename>`
- `display/YYYY-MM-DD/<filename>`
- `original/YYYY-MM-DD/<filename>`

### 步骤 4：导入 PostgreSQL

```bash
python scripts/import_pg.py
```

常用参数（可覆盖 `settings.yaml`）：

```bash
python scripts/import_pg.py \
  --input-jsonl output/metadata/photos_ai_oss.jsonl \
  --failed-jsonl output/metadata/import_pg_failed.jsonl \
  --db-host 127.0.0.1 \
  --db-port 5432 \
  --db-name luke-chu-site \
  --db-user admin \
  --db-password 1234
```

## 失败文件与重跑策略

- `process_images.py`：`output/metadata/failed.jsonl`
- `generate_ai_metadata.py`：`ai_failed_jsonl`
- `upload_to_oss.py`：`photos_ai_oss_failed.jsonl`
- `import_pg.py`：`import_pg_failed.jsonl`

建议：

- 大批处理场景优先保留每阶段产物，不要覆盖中间文件
- AI 阶段可结合 `ai_resume` 与 `ai_skip_completed` 控制断点续跑
- OSS 阶段可用 `--skip-existing-urls` 降低重复上传

## 常见问题

### 1) 导入后时间不是本地时区

- 在 `settings.yaml` 设置 `pg_timezone: "Asia/Shanghai"`
- `import_pg.py` 会自动执行会话级时区设置
- 另外建议在数据库层配置：

```sql
ALTER DATABASE "luke-chu-site" SET timezone TO 'Asia/Shanghai';
ALTER ROLE admin IN DATABASE "luke-chu-site" SET timezone TO 'Asia/Shanghai';
```

### 2) `upload_to_oss.py` 提示缺少 `oss2`

安装依赖：

```bash
pip install oss2
```

### 3) `generate_ai_metadata.py` 模型加载失败

- 检查 `settings.yaml` 中 `qwen_model_path` / `qwen_processor_path`
- 检查 `torch`、`transformers` 版本与设备环境

### 4) 标签类型冲突警告（`Tag type mismatch`）

这是由 `tags.name` 全局唯一导致：同名标签同时出现在不同类型（subject/element/mood）时会保留已存在类型并告警。

## 安全建议

- `.env` 含 OSS 凭证，禁止提交到远程仓库
- 使用最小权限的 AccessKey
- 定期轮换密钥

## 脚本职责速查

- `extract_exif.py`：提供 EXIF 解析能力（被 `process_images.py` 调用）
- `process_images.py`：图片预处理 + 初始元数据生成
- `generate_ai_metadata.py`：AI 文案与标签生成
- `upload_to_oss.py`：上传图片并回填 URL
- `import_pg.py`：将最终 JSONL 导入 PostgreSQL
