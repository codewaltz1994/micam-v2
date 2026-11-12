# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
在 Miloco 1.x 上补一个 /api/miot/ws/audio_stream 音频端点。

上游 1.x 只暴露 /ws/video_stream，音频端点是 2.0 才引入的，所以线上 1.x 镜像
连这个地址会拿到 403（Starlette 对不存在的 WS 路由直接拒绝）。

这里的做法是复用 SDK 自己的音频解码链路：
  miot_kit MIoTCameraInstance.register_decode_pcm_async(cb, channel)
  → 内部 MIoTMediaDecoder 把 G.711 / OPUS 解成 16kHz/s16/mono 的裸 PCM
因此不需要在服务端再碰 ffmpeg 或编解码，只做「订阅 + 转发」。

协议与 2.x 对齐，仅 codec 固定为 pcm：
  server → client  {"type":"init","codec":"pcm","sampleRate":16000,"numberOfChannels":1}
  server → client  <binary: PCM s16le / 16kHz / mono>
init 在「首帧到达时」才发，这样相机没有音频帧时客户端能靠自己的超时降级成纯视频，
不会挂着一条永远没数据的音轨。
"""

import asyncio
import json
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, WebSocket
from fastapi.websockets import WebSocketDisconnect, WebSocketState

from miloco_server.middleware import verify_websocket_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/miot", tags=["Xiaomi IoT Audio"])

# SDK 侧 AudioResampler(format="s16", layout="mono", rate=16000) 固定输出这个格式
PCM_SAMPLE_RATE = 16000
PCM_CHANNELS = 1
_PCM_INIT_MESSAGE = json.dumps(
    {
        "type": "init",
        "codec": "pcm",
        "sampleRate": PCM_SAMPLE_RATE,
        "numberOfChannels": PCM_CHANNELS,
    }
)

# 相机实例通常早已由 refresh_cameras 创建；这里留一点余量应对冷启动竞态，
# 但必须小于客户端（micam）等待 init 的 10s，否则客户端先超时白白多等一轮。
CAMERA_READY_TIMEOUT = 8.0
CAMERA_READY_POLL_INTERVAL = 0.5
CAMERA_CONNECT_COUNT_MAX = 4


class _AudioSubscriber:
    """一路音频 WS 订阅方。"""

    __slots__ = ("ws", "user_name", "token_hash", "cid", "init_sent")

    def __init__(self, ws: WebSocket, user_name: str, token_hash: str, cid: str):
        self.ws = ws
        self.user_name = user_name
        self.token_hash = token_hash
        self.cid = cid
        self.init_sent = False


class MIoTAudioStreamManager:
    """MIoT 音频 WS 管理器。

    同一路相机（camera_id.channel）只向 SDK 注册一次 decode_pcm 回调，
    帧回调里广播给所有订阅方——SDK 的 register_decode_pcm_async 是单槽
    （multi_reg 的 reg_id 计算在 1.x 里有缺陷），重复注册会互相覆盖。
    """

    def __init__(self) -> None:
        # key=camera_id.channel, value={user_tag: {cid: _AudioSubscriber}}
        self._connect_map: Dict[str, Dict[str, Dict[str, _AudioSubscriber]]] = {}
        # key=camera_id.channel, value=已注册回调的 MIoTCameraInstance
        # 存实例引用而不只是个标记:相机被移除再重新添加时 instance 会换新,
        # 旧注册随旧实例一起失效,必须能识别出来重新注册(对应 2.x 的 resubscribe)
        self._subscribed: Dict[str, object] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._connect_id: int = 0
        logger.info("Init MIoT Audio WS Manager (1.x backport)")

    # ---------------------------------------------------------------- helpers

    def _lock_for(self, camera_tag: str) -> asyncio.Lock:
        return self._locks.setdefault(camera_tag, asyncio.Lock())

    @staticmethod
    def _get_camera_instance(camera_id: str):
        """从 miot_proxy 里拿到 SDK 的 MIoTCameraInstance。"""
        from miloco_server.service.manager import get_manager

        miot_proxy = getattr(get_manager(), "miot_proxy", None)
        if miot_proxy is None:
            return None
        handlers = getattr(miot_proxy, "_camera_img_managers", None) or {}
        handler = handlers.get(camera_id)
        if handler is None:
            return None
        return getattr(handler, "miot_camera_instance", None)

    def _subscribers(self, camera_tag: str) -> list:
        return [
            sub
            for conn in self._connect_map.get(camera_tag, {}).values()
            for sub in conn.values()
        ]

    async def _wait_camera_instance(self, camera_id: str):
        """等相机实例就绪；调用方必须已持有该 camera_tag 的锁。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CAMERA_READY_TIMEOUT
        while True:
            instance = self._get_camera_instance(camera_id)
            if instance is not None:
                return instance
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(CAMERA_READY_POLL_INTERVAL)

    async def _subscribe_locked(
        self, camera_tag: str, instance, channel: int
    ) -> bool:
        """向 SDK 注册 decode_pcm；调用方必须已持有该 camera_tag 的锁。"""
        if self._subscribed.get(camera_tag) is instance:
            return True

        try:
            await instance.register_decode_pcm_async(self.__on_pcm, channel=channel)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error("Register decode_pcm failed, %s: %s", camera_tag, err)
            self._subscribed.pop(camera_tag, None)
            return False

        self._subscribed[camera_tag] = instance
        logger.info("Start audio stream, %s", camera_tag)
        return True

    async def _stop_subscribed(self, camera_tag: str) -> None:
        """无人订阅时退订，避免白白解码占 CPU。"""
        async with self._lock_for(camera_tag):
            # 抢锁期间可能刚有新连接登记进来，必须复查
            if self._subscribers(camera_tag):
                return
            instance = self._subscribed.pop(camera_tag, None)
            if instance is None:
                return

        _, _, channel_text = camera_tag.rpartition(".")
        try:
            channel = int(channel_text)
        except ValueError:
            return

        try:
            await instance.unregister_decode_pcm_async(channel=channel)
            logger.info("Stop audio stream, %s", camera_tag)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.warning("Unregister decode_pcm failed, %s: %s", camera_tag, err)

    # ------------------------------------------------------------- lifecycle

    async def new_connection(
        self,
        websocket: WebSocket,
        user_name: str,
        token_hash: str,
        camera_id: str,
        channel: int,
    ) -> Optional[str]:
        """登记一路音频 WS 订阅；相机不可用时返回 None。"""
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        # 登记订阅方与注册 SDK 回调必须在同一把锁内：如果先注册后登记，
        # 注册后立刻到达的首帧会看到「没有订阅方」而把回调退订掉，
        # 客户端随后一直等不到 init，只能降级成纯视频。
        async with self._lock_for(camera_tag):
            instance = await self._wait_camera_instance(camera_id)
            if instance is None:
                logger.warning(
                    "Camera instance not ready, cannot start audio stream, %s",
                    camera_tag,
                )
                return None

            connection_id = str(self._connect_id)
            self._connect_id += 1
            sub = _AudioSubscriber(websocket, user_name, token_hash, connection_id)
            conns = self._connect_map.setdefault(camera_tag, {}).setdefault(
                user_tag, {}
            )
            conns[connection_id] = sub
            if len(conns) > CAMERA_CONNECT_COUNT_MAX:
                logger.warning(
                    "Too many audio connections, %s, %s, remove first",
                    camera_tag,
                    user_tag,
                )
                _, stale = next(iter(conns.items()))
                conns.pop(stale.cid, None)
                try:
                    await stale.ws.close()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

            if not await self._subscribe_locked(camera_tag, instance, channel):
                conns.pop(connection_id, None)
                if not conns:
                    self._connect_map.get(camera_tag, {}).pop(user_tag, None)
                if not self._connect_map.get(camera_tag):
                    self._connect_map.pop(camera_tag, None)
                return None

        logger.info(
            "New audio stream connection, %s, %s, %s",
            camera_tag,
            user_tag,
            connection_id,
        )
        return connection_id

    async def close_connection(
        self,
        user_name: str,
        token_hash: str,
        camera_id: str,
        channel: int,
        cid: str,
    ) -> None:
        """移除一路订阅；最后一路断开时退订 SDK 回调。"""
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        camera_conns = self._connect_map.get(camera_tag)
        if not camera_conns:
            return
        conns = camera_conns.get(user_tag)
        if not conns or cid not in conns:
            return

        sub = conns.pop(cid)
        if sub.ws.client_state == WebSocketState.CONNECTED:
            try:
                await sub.ws.close()
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.warning("Audio WebSocket close error, %s: %s", camera_tag, err)
        logger.info("Close audio stream connection, %s, %s", camera_tag, cid)

        if not conns:
            camera_conns.pop(user_tag, None)
        if not camera_conns:
            self._connect_map.pop(camera_tag, None)
            await self._stop_subscribed(camera_tag)

    # ------------------------------------------------------------------ data

    async def __on_pcm(
        self, did: str, data: bytes, ts: int, channel: int
    ) -> None:
        """SDK 解码后的 PCM 帧回调（在 main loop 上执行）。"""
        camera_tag = f"{did}.{channel}"
        subscribers = self._subscribers(camera_tag)
        if not subscribers:
            await self._stop_subscribed(camera_tag)
            return
        # decoder 在 frames 为空时也会回调空 bytes，别把它当首帧
        if not data:
            return

        for sub in subscribers:
            try:
                # 首帧到了才发 init：相机没有音频时客户端会超时降级为纯视频
                if not sub.init_sent:
                    await sub.ws.send_text(_PCM_INIT_MESSAGE)
                    sub.init_sent = True
                await sub.ws.send_bytes(data)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Audio WebSocket send error, %s, %s: %s", camera_tag, sub.cid, err
                )
                await self.close_connection(
                    sub.user_name, sub.token_hash, did, channel, sub.cid
                )


miot_audio_stream_manager = MIoTAudioStreamManager()


@router.websocket("/ws/audio_stream")
async def audio_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    channel: int,
    current_user: str = Depends(verify_websocket_token),
):
    """Audio stream WebSocket. 与 1.x 视频端点保持同一套 cookie 鉴权。"""
    logger.info(
        "Audio WebSocket connection request, %s, %s.%d",
        current_user,
        camera_id,
        channel,
    )
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: Optional[str] = None
    try:
        await websocket.accept()
        cid = await miot_audio_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel,
        )
        if cid is None:
            # 相机不可用：明确关闭，让客户端立刻降级为纯视频而不是干等
            await websocket.close(code=1013, reason="Camera not ready for audio")
            return
        while True:
            try:
                message = await websocket.receive_text()
                logger.debug("Received message from audio client, %s", message)
            except WebSocketDisconnect:
                logger.info("Audio client closed, %s.%d", camera_id, channel)
                break
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error("Audio WebSocket error: %s", err)
                break
    except WebSocketDisconnect:
        logger.info("Audio client disconnected, %s.%d", camera_id, channel)
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.error("Audio WebSocket error, %s", err)
        try:
            await websocket.close(code=1011, reason=f"Server error: {err}")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    finally:
        if cid:
            await miot_audio_stream_manager.close_connection(
                user_name=current_user,
                token_hash=token_hash,
                camera_id=camera_id,
                channel=channel,
                cid=cid,
            )
