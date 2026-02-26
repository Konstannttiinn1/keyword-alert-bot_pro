import pickle
import re
from pathlib import Path


URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
NUM_RE = re.compile(r"\d+")
SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = URL_RE.sub(" <URL> ", text)
    text = NUM_RE.sub(" <NUM> ", text)
    text = SPACE_RE.sub(" ", text).strip()
    return text


class RelevanceFilter:
    def __init__(self, model_path: str):
        self.model_path = Path(model_path)
        self.model = None

    def load(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            import joblib  # type: ignore

            self.model = joblib.load(self.model_path)
        except Exception:
            with self.model_path.open("rb") as f:
                self.model = pickle.load(f)
        return True

    def predict_score(self, text: str) -> float:
        if self.model is None:
            raise RuntimeError("Модель не загружена")
        clean = normalize_text(text)
        proba = self.model.predict_proba([clean])[0][1]
        return float(proba)
