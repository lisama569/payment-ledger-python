"""服务入口。

本地启动::

    uv run fastapi dev main.py            # 热重载开发模式
    uv run fastapi run main.py --port 8000
    LEDGER_FILE=/tmp/ledger.json uv run fastapi run main.py

数据文件默认落在 data/ledger.json，可用环境变量 LEDGER_FILE 覆盖。
"""

from app.api import app  # noqa: F401  # 供 uvicorn/gunicorn 加载
