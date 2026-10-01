from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .models import AppSettings


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"


@dataclass(frozen=True)
class ConfigLoadResult:
    settings: AppSettings
    error: str | None = None


class ConfigRepository:
    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path

    def load(self) -> ConfigLoadResult:
        if not self.path.exists():
            return ConfigLoadResult(AppSettings())
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return ConfigLoadResult(AppSettings.model_validate(raw))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            return ConfigLoadResult(
                AppSettings(),
                f"Файл настроек повреждён или недоступен: {exc}. Загружены значения по умолчанию.",
            )

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        payload = settings.model_dump_json(indent=2)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                raise

