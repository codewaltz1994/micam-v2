#!/usr/bin/env python3
"""音频端点逻辑自检（不需要真实相机 / 小米账号）。

在补丁镜像里跑：

    docker run --rm -v "$PWD/verify.py:/tmp/verify.py:ro" \
        --entrypoint python3 miloco:local-arm64 /tmp/verify.py

验证的是 audio_controller 里「向 SDK 订阅一次 → 首帧补发 init → 广播 → 最后
一路断开后退订」这条主路径；相机实例被替换成假对象，因此不需要连摄像头。
"""

import asyncio
import json
import sys

from miloco_server.controller import audio_controller as ac
from fastapi.websockets import WebSocketState


class FakeWebSocket:
    client_state = WebSocketState.CONNECTED

    def __init__(self) -> None:
        self.texts = []
        self.binaries = []

    async def send_text(self, data: str) -> None:
        self.texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binaries.append(data)

    async def close(self, *args, **kwargs) -> None:
        self.client_state = WebSocketState.DISCONNECTED


class FakeCameraInstance:
    def __init__(self) -> None:
        self.callback = None
        self.registers = 0
        self.unregisters = 0

    async def register_decode_pcm_async(self, callback, channel=0, multi_reg=False):
        self.callback = callback
        self.registers += 1
        return 0

    async def unregister_decode_pcm_async(self, channel=0, reg_id=0):
        self.callback = None
        self.unregisters += 1

    async def emit(self, data: bytes) -> None:
        await self.callback("cam1", data, 0, 0)


def main() -> int:
    instance = FakeCameraInstance()
    ac.MIoTAudioStreamManager._get_camera_instance = staticmethod(
        lambda camera_id: instance if camera_id == "cam1" else None
    )
    # 相机不存在的那条断言不必真等 8 秒
    ac.CAMERA_READY_TIMEOUT = 0.1

    manager = ac.MIoTAudioStreamManager()

    async def scenario() -> None:
        first, second = FakeWebSocket(), FakeWebSocket()

        cid1 = await manager.new_connection(first, "admin", "h", "cam1", 0)
        cid2 = await manager.new_connection(second, "admin", "h", "cam1", 0)
        assert cid1 and cid2, "new_connection 应返回连接 id"
        assert instance.registers == 1, f"只应向 SDK 注册一次，实际 {instance.registers}"

        await instance.emit(b"\x01\x02" * 320)
        await instance.emit(b"\x03\x04" * 320)

        for ws in (first, second):
            assert len(ws.texts) == 1, f"init 只应发一次，实际 {len(ws.texts)}"
            init = json.loads(ws.texts[0])
            assert init == {
                "type": "init",
                "codec": "pcm",
                "sampleRate": 16000,
                "numberOfChannels": 1,
            }, init
            assert len(ws.binaries) == 2, f"两帧 PCM 应全部转发，实际 {len(ws.binaries)}"

        # 空 PCM（解码器没解出帧时也会回调）不应被当成首帧或数据帧
        empty = FakeWebSocket()
        cid3 = await manager.new_connection(empty, "admin", "h", "cam1", 0)
        await instance.emit(b"")
        assert empty.texts == [] and empty.binaries == [], "空帧被转发了"

        await manager.close_connection("admin", "h", "cam1", 0, cid3)
        assert instance.unregisters == 0, "还有连接时不应退订"

        await manager.close_connection("admin", "h", "cam1", 0, cid1)
        await manager.close_connection("admin", "h", "cam1", 0, cid2)
        assert instance.unregisters == 1, f"最后一路断开应退订一次，实际 {instance.unregisters}"

        # 相机不存在时必须快速失败（返回 None），让客户端降级为纯视频
        assert await manager.new_connection(FakeWebSocket(), "admin", "h", "camX", 0) is None

    asyncio.run(scenario())
    print("OK: audio_controller 订阅/初始化/广播/退订逻辑通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
