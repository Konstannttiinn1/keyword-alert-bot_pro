import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, recall_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline

from filters.relevance import normalize_text

BASE_DIR = Path(__file__).resolve().parent


def load_dataset(path: Path):
    texts, labels = [], []
    if not path.exists():
        return texts, labels
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            label = row.get("label")
            text = row.get("text", "")
            if label in (0, 1) and text.strip():
                texts.append(normalize_text(text))
                labels.append(label)
    return texts, labels


def build_pipeline():
    return Pipeline(
        steps=[
            (
                "features",
                FeatureUnion(
                    [
                        ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)),
                        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1)),
                    ]
                ),
            ),
            ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()

    dataset_path = BASE_DIR / "data" / args.tenant / "dataset.jsonl"
    texts, labels = load_dataset(dataset_path)
    if len(set(labels)) < 2:
        raise RuntimeError("Нужно минимум 2 класса (0 и 1) в dataset.jsonl")

    x_train, x_test, y_train, y_test = train_test_split(texts, labels, test_size=0.25, random_state=42, stratify=labels)
    pipeline = build_pipeline()
    pipeline.fit(x_train, y_train)

    y_pred = pipeline.predict(x_test)
    recall_pos = recall_score(y_test, y_pred, pos_label=1)
    report = classification_report(y_test, y_pred, output_dict=True)

    model_dir = BASE_DIR / "models" / args.tenant
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "relevance.joblib"
    metadata_path = model_dir / "metadata.json"

    joblib.dump(pipeline, model_path)
    metadata = {
        "date": datetime.now(timezone.utc).isoformat(),
        "dataset_size": len(labels),
        "metrics": {
            "recall_label_1": recall_pos,
            "classification_report": report,
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Dataset size: {len(labels)}")
    print(f"Recall(label=1): {recall_pos:.4f}")
    print("Model saved:", model_path)


if __name__ == "__main__":
    main()
