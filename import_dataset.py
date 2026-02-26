import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from docx import Document

BASE_DIR = Path(__file__).resolve().parent


def read_txt(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def clean_docx_line(line: str) -> str:
    line = line.strip()
    match = re.match(r"^\s*текст\s*:\s*(.*)$", line, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return line


def read_docx(path: Path) -> list[str]:
    doc = Document(path)
    out = []
    for p in doc.paragraphs:
        text = clean_docx_line(p.text)
        if text:
            out.append(text)
    return out


def read_messages(path: Path) -> list[str]:
    if path.suffix.lower() == ".txt":
        return read_txt(path)
    if path.suffix.lower() == ".docx":
        return read_docx(path)
    raise ValueError(f"Неподдерживаемый формат: {path}")


def append_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--relevant", required=True)
    parser.add_argument("--not_relevant", required=True)
    args = parser.parse_args()

    dataset_path = BASE_DIR / "data" / args.tenant / "dataset.jsonl"
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for text in read_messages(Path(args.relevant)):
        rows.append({
            "tenant_id": args.tenant,
            "text": text,
            "label": 1,
            "keyword": "bulk_import",
            "chat_id": None,
            "message_id": None,
            "is_forward": False,
            "ts": now,
            "source": "bulk_import",
        })

    for text in read_messages(Path(args.not_relevant)):
        rows.append({
            "tenant_id": args.tenant,
            "text": text,
            "label": 0,
            "keyword": "bulk_import",
            "chat_id": None,
            "message_id": None,
            "is_forward": False,
            "ts": now,
            "source": "bulk_import",
        })

    append_jsonl(dataset_path, rows)
    print(f"Импортировано записей: {len(rows)}")


if __name__ == "__main__":
    main()
