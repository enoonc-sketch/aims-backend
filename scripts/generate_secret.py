#!/usr/bin/env python3
"""打印一个适合作为 SECRET_KEY 的随机值(64 字节随机数, urlsafe base64)。

这是**部署时**由你运行一次、把结果注入环境变量 / .env / secret 存储的辅助脚本;
应用本身不会生成或保存任何密钥。
"""
import secrets

print(secrets.token_urlsafe(64))
