#!/usr/bin/env python3
"""生成仅供本机开发/测试的自签名证书到 ./certs/ (实现在 app/cli/devcert.py)。

然后在 .env 里设置: SSL_CERTFILE=certs/dev-cert.pem  SSL_KEYFILE=certs/dev-key.pem
(或者直接运行 `python run_standalone.py setup`, 它会替你做完这些。)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli.devcert import generate_dev_cert  # noqa: E402

cert, key = generate_dev_cert(Path("certs"))
print(f"已生成: {cert}  {key}  (有效期 365 天, 仅限本机开发)")
