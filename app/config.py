import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "血缘水印台"
    SECRET = os.environ.get("SECRET_KEY", "cc-watermark-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "watermark_lab.db")
    # 并发纠偏抢写锁的等待上限（毫秒）
    BUSY_TIMEOUT_MS = int(os.environ.get("BUSY_TIMEOUT_MS", "5000"))

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)
