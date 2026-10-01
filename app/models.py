from __future__ import annotations

import math
from ipaddress import IPv4Address

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CameraSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str = "0.0.0.0"
    port: int = Field(default=2002, ge=1, le=65535)

    @field_validator("ip")
    @classmethod
    def validate_ipv4(cls, value: str) -> str:
        try:
            return str(IPv4Address(value))
        except ValueError as exc:
            raise ValueError("Требуется корректный IPv4-адрес") from exc


def default_cameras() -> list[CameraSettings]:
    return [CameraSettings() for _ in range(4)]


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_count: int = Field(default=1, ge=1, le=4)
    expected_codes: int = Field(default=1, ge=1, le=4)
    collection_window: float = Field(default=3.0, gt=0)
    cameras: list[CameraSettings] = Field(default_factory=default_cameras)

    @field_validator("collection_window")
    @classmethod
    def validate_finite_window(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Окно сбора должно быть конечным положительным числом")
        return value

    @field_validator("cameras")
    @classmethod
    def validate_four_cameras(cls, value: list[CameraSettings]) -> list[CameraSettings]:
        if len(value) != 4:
            raise ValueError("Должны быть заданы настройки ровно четырёх камер")
        return value

