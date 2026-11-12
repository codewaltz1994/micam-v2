#!/usr/bin/env python3
"""把 audio_controller 的 /ws/audio_stream 路由挂到 Miloco 1.x 的 FastAPI app 上。

只做两处最小插入（都在 /app/miloco_server/main.py）：
  1. 在 controller 的 import 之后引入 audio_router
  2. 在 app.include_router(miot_router, prefix="/api") 之后注册 audio_router

两处锚点都要求精确命中一次，否则构建失败——宁可构建报错，也不要
产出一个表面正常、实际仍然 403 的镜像。
"""

import sys
from pathlib import Path

TARGET = Path("/app/miloco_server/main.py")

OLD_IMPORT_ANCHOR = """from miloco_server.middleware.auth_middleware import AuthStaticFiles
"""

NEW_IMPORT = """from miloco_server.middleware.auth_middleware import AuthStaticFiles
# 1.x 音频补丁：上游 1.x 只有 /ws/video_stream，音频端点是 2.0 才有的
from miloco_server.controller.audio_controller import router as miot_audio_router
"""

OLD_ROUTE_ANCHOR = """app.include_router(miot_router, prefix="/api")
"""

NEW_ROUTE = """app.include_router(miot_router, prefix="/api")
app.include_router(miot_audio_router, prefix="/api")
"""


def main() -> int:
    if not TARGET.is_file():
        print(f"[patch_main] target not found: {TARGET}", file=sys.stderr)
        return 1

    source = TARGET.read_text(encoding="utf-8")
    if "miot_audio_router" in source:
        print("[patch_main] already patched, skip")
        return 0

    for name, anchor in (
        ("import", OLD_IMPORT_ANCHOR),
        ("include_router", OLD_ROUTE_ANCHOR),
    ):
        hits = source.count(anchor)
        if hits != 1:
            print(
                f"[patch_main] {name} anchor matched {hits} times (expected 1); "
                "upstream layout changed, refusing to patch",
                file=sys.stderr,
            )
            return 1

    source = source.replace(OLD_IMPORT_ANCHOR, NEW_IMPORT)
    source = source.replace(OLD_ROUTE_ANCHOR, NEW_ROUTE)
    TARGET.write_text(source, encoding="utf-8")
    print("[patch_main] /api/miot/ws/audio_stream route registered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
