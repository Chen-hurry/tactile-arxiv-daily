#!/usr/bin/env bash
# 本地浏览：http://localhost:${PORT:-8765}（也可直接用浏览器打开 docs/index.html）
cd "$(dirname "$0")/../docs" && exec python3 -m http.server "${PORT:-8765}"
