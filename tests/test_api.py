from fastapi.testclient import TestClient

from app.config import ConfigRepository
from app.main import create_app


def test_health_settings_and_validation(tmp_path):
    app = create_app(ConfigRepository(tmp_path / "settings.json"))
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        defaults = client.get("/api/settings").json()["settings"]
        assert defaults["camera_count"] == 1
        assert client.put("/api/settings", json={**defaults, "camera_count": 5}).status_code == 422
        assert client.put("/api/settings", json={**defaults, "collection_window": "NaN"}).status_code == 422
        assert client.put("/api/settings", json={**defaults, "cameras": [{"ip": "999.1.1.1", "port": 2002}] * 4}).status_code == 422
        response = client.put("/api/settings", json={**defaults, "expected_codes": 2})
        assert response.status_code == 200
        assert client.get("/api/state").json()["cycle"]["expected"] == 2
        response = client.post("/api/cameras/disconnect")
        assert response.status_code == 200
        assert response.json()["cameras"][0]["status"] == "disconnected"


def test_index_and_static_assets(tmp_path):
    app = create_app(ConfigRepository(tmp_path / "settings.json"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "Подключение камер" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/stage2.css").status_code == 200
