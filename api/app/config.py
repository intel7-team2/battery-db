"""환경 변수 설정. 기본값은 VM 배포 기준 (DB와 같은 서버, 유닉스 소켓 + peer 인증)."""
import os
from pathlib import Path

DB_HOST = os.environ.get("BATTERY_DB_HOST", "/var/run/postgresql")
DB_PORT = int(os.environ.get("BATTERY_DB_PORT", "5432"))
DB_NAME = os.environ.get("BATTERY_DB_NAME", "battery")
KEYS_FILE = Path(os.environ.get("BATTERY_API_KEYS", "/etc/battery-api/keys.json"))

# 브라우저(HMI 웹 등)에서 직접 부를 때만. 쉼표로 구분. 비우면 CORS 끔.
CORS_ORIGINS = [o.strip() for o in os.environ.get("BATTERY_API_CORS", "").split(",") if o.strip()]


def db_password(role: str) -> str | None:
    """VM에서는 peer 인증이라 필요 없음. PC에서 터널로 띄울 때만 BATTERY_DB_PASSWORD_READER 등."""
    return os.environ.get(f"BATTERY_DB_PASSWORD_{role.upper()}")
