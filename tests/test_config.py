import json

from app.config import ConfigRepository
from app.models import AppSettings, CameraSettings


def test_defaults_when_file_is_missing(tmp_path):
    result = ConfigRepository(tmp_path / "settings.json").load()
    assert result.error is None
    assert result.settings.camera_count == 1
    assert [camera.ip for camera in result.settings.cameras] == ["0.0.0.0"] * 4


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    repository = ConfigRepository(path)
    settings = AppSettings(
        camera_count=2,
        expected_codes=3,
        collection_window=1.5,
        cameras=[CameraSettings(ip=f"192.168.1.{index}", port=2000 + index) for index in range(1, 5)],
    )

    repository.save(settings)
    loaded = repository.load()

    assert loaded.error is None
    assert loaded.settings == settings
    assert not path.with_name("settings.json.tmp").exists()


def test_corrupt_file_is_preserved_until_explicit_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    repository = ConfigRepository(path)

    loaded = repository.load()

    assert loaded.error is not None
    assert loaded.settings == AppSettings()
    assert path.read_text(encoding="utf-8") == "{broken"

    repository.save(loaded.settings)
    assert json.loads(path.read_text(encoding="utf-8"))["camera_count"] == 1

