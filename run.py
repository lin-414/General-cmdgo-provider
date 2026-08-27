#!/usr/bin/env python3
"""
独立运行入口（不依赖 Hermes Agent 也能跑）。

用法：
  python run.py                      # 用环境变量/默认值在前台启动
  python run.py --port 8787 --api-key sk-xxx
  python run.py --base-url https://api.commandcode.ai --cc-version 1.31.0

它启动本地 OpenAI 兼容代理服务器。
"""
import importlib.util
import os
import sys

# In a PyInstaller one-file build, bundled files live under sys._MEIPASS.
_HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_INIT = os.path.join(_HERE, "cmdgo_provider.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("cmdgo_provider_standalone", _INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    mod = _load_module()
    mod.main()
