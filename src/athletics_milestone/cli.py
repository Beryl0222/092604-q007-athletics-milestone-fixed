"""从标准输入接收 JSON 请求。"""
import json
import sys

from .api import handle


def main() -> int:
    raw = sys.stdin.read().strip() or '{"action":"health"}'
    try:
        print(handle(raw))
    except Exception as exc:  # 边界统一转成错误信封，便于调用方解析
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
