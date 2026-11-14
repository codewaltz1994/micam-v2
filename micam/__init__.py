import os
import json
import time
import asyncio
import aiohttp
import argparse
import logging
import threading
import subprocess
from typing import Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Miloco 的音频不是混在视频 WS 里的,而是独立端点 /ws/audio_stream,
# 首帧前先发一条 init 文本信令再发裸音频包:
#   {"type": "init", "codec": "g711a", "sampleRate": 8000, "numberOfChannels": 1}
# codec -> (ffmpeg 输入格式, 采样率, 声道数)
AUDIO_INPUT_FORMATS = {
    "g711a": ("alaw", 8000, 1),
    "g711u": ("mulaw", 8000, 1),
    # Miloco 1.x 没有音频端点,服务端补丁用 SDK 的 register_decode_pcm_async
    # 把 G.711 解成 16kHz/s16/mono 裸 PCM 再下发,codec 名固定为 "pcm"。
    "pcm": ("s16le", 16000, 1),
}
AUDIO_INIT_TIMEOUT = 10.0
AUDIO_DATA_TIMEOUT = 60.0

# 裸音频格式 -> (每样本字节数, 静音字节)
AUDIO_SAMPLE_INFO = {
    "s16le": (2, b"\x00"),
    "alaw": (1, b"\xd5"),
    "mulaw": (1, b"\xff"),
}
# 音频不能用 -use_wallclock_as_timestamps: ffmpeg 8 的调度器会暂停「跑在前面」的输入,
# 被暂停那路积压的数据读出来时又被打上更晚的墙钟时间戳 -> 更超前 -> 再被暂停,
# 视频 stdin 因此周期性卡住 15~20s,服务端 WS ping 超时断开。
# 所以音频按样本数计时,由 micam 严格按实时节拍往管道里写: 缺数据补静音,多了丢最旧的,
# 保证音频时间线永远等于真实流逝时间,不会拖住视频。
AUDIO_PUMP_INTERVAL = 0.02
AUDIO_JITTER_TARGET = 0.2  # 抖动缓冲: 攒够这么多再开始出真实音频
AUDIO_JITTER_MAX = 1.0  # 超过就丢最旧的,回落到 target,防止延迟越积越大

# 看门狗: 视频连续这么久没成功写进 ffmpeg(相机掉线、ffmpeg 卡死、事件循环被阻塞……)
# 就直接结束进程,交给容器 restart 策略重连。用独立线程实现,事件循环卡死也能触发。
STALL_TIMEOUT = 90.0


class RTSPBridge:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        camera_id: str,
        rtsp_url: str,
        video_codec='hevc',
        channel=0,
        enable_audio=True,
    ):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.camera_id = camera_id
        self.channel = channel
        self.video_codec = video_codec
        self.rtsp_url = rtsp_url
        self.enable_audio = enable_audio
        self.process: Optional[subprocess.Popen] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.waiting_for_keyframe = True
        self.audio_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.audio_input: Optional[tuple] = None
        self.audio_fd: Optional[int] = None
        self.audio_buffer = bytearray()
        self.audio_ready = asyncio.Event()
        self.last_progress = time.monotonic()

    def _watchdog(self):
        while True:
            time.sleep(5)
            idle = time.monotonic() - self.last_progress
            if idle < STALL_TIMEOUT:
                continue
            logger.error("No video written to FFmpeg for %.0fs, exiting for restart", idle)
            process = self.process
            if process is not None:
                try:
                    process.kill()
                except OSError:
                    pass
            os._exit(1)

    @staticmethod
    def _log_ffmpeg_stderr(stream):
        # 持续读走 stderr: 既能实时看到 ffmpeg 报错,也避免管道写满把 ffmpeg 卡住
        for line in iter(stream.readline, b""):
            line = line.decode(errors="replace").rstrip()
            if line:
                logger.error("FFmpeg: %s", line)

    async def _login(self) -> bool:
        """Login and retrieve access token."""
        login_url = f"{self.base_url}/api/auth/login"
        payload = {"username": self.username, "password": self.password}

        try:
            async with self.session.post(login_url, json=payload, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Login successful. %s", data)

                    # Call login_status to ensure session is active
                    status_url = f"{self.base_url}/api/miot/login_status"
                    async with self.session.get(status_url, ssl=False) as status_resp:
                        if status_resp.status == 200:
                            return True
                        else:
                            logger.error(f"Login status check failed: {status_resp.status}")
                            return False
                else:
                    logger.error(f"Login failed: {response.status} - {await response.text()}")
                    return False
        except Exception as e:
            logger.error(f"Login exception: {e}")
            await asyncio.sleep(3)
            return False

    def _start_ffmpeg(self):
        """Start FFmpeg process."""
        # FFmpeg command to read from stdin and publish to RTSP
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-v', 'error',
            '-hide_banner',
            '-use_wallclock_as_timestamps', '1',  # Generate timestamps from arrival time
            '-analyzeduration', '20000000',  # 20 seconds
            '-probesize', '20000000',  # 20 MB
            '-f', self.video_codec,  # Input format
            '-i', 'pipe:0',  # Read from stdin
        ]

        pass_fds = ()
        if self.audio_input:
            audio_format, audio_rate, audio_channels = self.audio_input
            # 音频走独立管道,不能用 stdin;用 fd 传给 ffmpeg 的 pipe:<fd> 输入
            audio_read_fd, self.audio_fd = os.pipe()
            pass_fds = (audio_read_fd,)
            ffmpeg_cmd += [
                # 不加 -use_wallclock_as_timestamps: 时间戳按样本数算,节拍由 _audio_pump 保证
                '-f', audio_format,  # 裸音频,无容器
                '-ar', str(audio_rate),
                '-ac', str(audio_channels),
                '-i', f'pipe:{audio_read_fd}',
                '-map', '0:v',
                '-map', '1:a',
                '-c:a', 'aac',  # 转 AAC,兼容 go2rtc / HA / Frigate / Scrypted
                '-b:a', '64k',
                '-ar', '48000',
                '-ac', '1',
            ]
        else:
            ffmpeg_cmd += ['-map', '0:v']

        ffmpeg_cmd += [
            '-c:v', 'copy',  # Copy video stream
            '-f', 'rtsp',  # Output format
            '-rtsp_transport', 'tcp',  # Use TCP for RTSP
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg: {' '.join(ffmpeg_cmd)}")
        self.process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,  # Suppress FFmpeg stdout
            stderr=subprocess.PIPE,  # Capture stderr for debugging if needed
            pass_fds=pass_fds,
        )
        threading.Thread(
            target=self._log_ffmpeg_stderr, args=(self.process.stderr,), daemon=True
        ).start()
        if pass_fds:
            # 读端已交给 ffmpeg,父进程不再持有,否则写端永远等不到 EOF
            os.close(pass_fds[0])

    def _stop_ffmpeg(self):
        """Stop FFmpeg process."""
        if self.audio_fd is not None:
            try:
                os.close(self.audio_fd)
            except OSError:
                pass
            self.audio_fd = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None

    def _ws_url(self, path: str) -> str:
        protocol = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://")[1]
        return f"{protocol}://{host}{path}?camera_id={self.camera_id}&channel={self.channel}"

    async def _open_audio_stream(self) -> Optional[aiohttp.ClientWebSocketResponse]:
        """订阅音频 WS;连不上就只推视频。init 信令由 _audio_loop 接收。"""
        ws_url = self._ws_url("/api/miot/ws/audio_stream")
        logger.info(f"Connecting to audio WebSocket: {ws_url}")
        try:
            return await self.session.ws_connect(ws_url, ssl=False)
        except Exception as e:
            logger.warning(f"Audio stream unavailable, streaming video only: {e}")
            return None

    def _parse_audio_init(self, msg) -> bool:
        if msg.type != aiohttp.WSMsgType.TEXT:
            logger.warning(f"Unexpected audio init message type: {msg.type}, video only")
            return False

        try:
            init = json.loads(msg.data)
        except (TypeError, ValueError):
            logger.warning(f"Invalid audio init message: {msg.data!r}, video only")
            return False

        codec = str(init.get("codec") or "").lower()
        audio_format = AUDIO_INPUT_FORMATS.get(codec)
        if audio_format is None:
            logger.warning(f"Unsupported audio codec '{codec}', streaming video only")
            return False

        # 采样率/声道以服务端 init 为准,ffmpeg 解裸流时这两个参数不能猜
        audio_fmt, default_rate, default_channels = audio_format
        try:
            rate = int(init.get("sampleRate") or default_rate)
            channels = int(init.get("numberOfChannels") or default_channels)
        except (TypeError, ValueError):
            rate, channels = default_rate, default_channels

        self.audio_input = (audio_fmt, rate, channels)
        logger.info(f"Audio codec: {codec} ({rate} Hz, {channels} ch)")
        return True

    async def _wait_audio_ready(self, audio_task: asyncio.Task) -> bool:
        """首个关键帧到达时再等音频 init。

        相机掉线期间重连时音视频都没数据,如果一连上就限时等 init,会把「相机离线」
        误判成「相机没音频」,相机恢复后就一直只有画面没声音。
        """
        if not self.audio_ready.is_set() and not audio_task.done():
            waiter = asyncio.create_task(self.audio_ready.wait())
            await asyncio.wait(
                {waiter, audio_task},
                timeout=AUDIO_INIT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            waiter.cancel()
        if self.audio_ready.is_set() and not audio_task.done():
            return True
        logger.warning("No audio from camera, streaming video only")
        return False

    def _audio_bytes_per_sec(self) -> tuple:
        audio_fmt, rate, channels = self.audio_input
        sample_bytes, silence = AUDIO_SAMPLE_INFO[audio_fmt]
        frame_bytes = sample_bytes * channels
        return rate, frame_bytes, silence

    async def _audio_loop(self):
        """先收 init 信令确定编码,之后把裸音频包放进抖动缓冲,真正写管道由 _audio_pump 按节拍完成。

        建连后就一直在 receive,既能及时回服务端的 ping,也能在等关键帧期间先攒好 init。
        """
        ws = self.audio_ws
        try:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=AUDIO_DATA_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Audio init timeout (camera may have no audio)")
                return
            if not self._parse_audio_init(msg):
                return
            self.audio_ready.set()

            rate, frame_bytes, _ = self._audio_bytes_per_sec()
            max_bytes = int(rate * AUDIO_JITTER_MAX) * frame_bytes
            target_bytes = int(rate * AUDIO_JITTER_TARGET) * frame_bytes
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=AUDIO_DATA_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.error("Audio data received timeout. Exiting.")
                    break

                if msg.type == aiohttp.WSMsgType.BINARY:
                    buf = self.audio_buffer
                    buf += msg.data
                    if len(buf) > max_bytes:
                        # 来得比实时快(相机时钟偏快 / 服务端积压后突发): 丢最旧的
                        drop = len(buf) - target_bytes
                        del buf[:drop - drop % frame_bytes]
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # init 之外的文本信令一律忽略
                    logger.debug("Audio control message: %s", msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.info("Audio WebSocket close(%s)", msg.type)
                    break
                else:
                    logger.debug("Unexpected audio message type: %s", msg.type)
        finally:
            # 音轨断了不降级成"有画面没声音":收掉 ffmpeg,交由容器重启策略重连。
            # ffmpeg 还没挂上音频输入(仍在等关键帧)时不动它,由 run 决定是否降级纯视频
            logger.info("Audio stream finished")
            if self.audio_fd is not None:
                self._stop_ffmpeg()

    async def _audio_pump(self):
        """按真实时间往 ffmpeg 音频管道写数据,保证音频时间线 == 流逝时间。

        从第一帧视频写入 ffmpeg 时开始计时,两路时间线起点一致才能音画同步。
        """
        rate, frame_bytes, silence = self._audio_bytes_per_sec()
        target_bytes = int(rate * AUDIO_JITTER_TARGET) * frame_bytes
        loop = asyncio.get_running_loop()
        start = loop.time()
        written = 0
        primed = False
        underruns = 0
        # 开始前攒下的旧数据和视频对不上,只留一个抖动缓冲的量
        buf = self.audio_buffer
        if len(buf) > target_bytes:
            del buf[:len(buf) - target_bytes]
        try:
            while True:
                due = int((loop.time() - start) * rate) * frame_bytes - written
                if due > 0:
                    if not primed and len(buf) >= target_bytes:
                        primed = True
                    take = min(due, len(buf)) if primed else 0
                    take -= take % frame_bytes
                    chunk = bytes(buf[:take])
                    del buf[:take]
                    if take < due:
                        # 数据不够就补静音: 绝不能让音频时间线落后,否则 ffmpeg 会卡住视频
                        chunk += silence * (due - take)
                        if primed:
                            primed = False
                            underruns += 1
                            if underruns == 1 or underruns % 100 == 0:
                                logger.info("Audio underrun x%d, padding silence", underruns)
                    await self.audio_write(chunk)
                    written += due
                await asyncio.sleep(AUDIO_PUMP_INTERVAL)
        except (BrokenPipeError, OSError) as e:
            logger.error("FFmpeg audio pipe closed. %s", e)
            self._stop_ffmpeg()

    async def audio_write(self, data):
        if self.audio_fd is None:
            raise BrokenPipeError("Audio pipe not opened")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._audio_write_all, self.audio_fd, data)

    @staticmethod
    def _audio_write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]

    async def run(self):
        """Main loop to connect to WebSocket and pipe data."""
        jar = aiohttp.CookieJar(unsafe=True)
        self.last_progress = time.monotonic()
        threading.Thread(target=self._watchdog, daemon=True).start()
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            self.session = session
            if not await self._login():
                return

            # 音频是独立端点,先订阅上;编码等首个关键帧到了再定,再决定 ffmpeg 挂不挂第二路输入
            audio_task: Optional[asyncio.Task] = None
            if self.enable_audio:
                self.audio_ws = await self._open_audio_stream()
                if self.audio_ws is not None:
                    audio_task = asyncio.create_task(self._audio_loop())
            pump_task: Optional[asyncio.Task] = None

            ws_url = self._ws_url("/api/miot/ws/video_stream")
            logger.info(f"Connecting to WebSocket: {ws_url}")

            try:
                async with session.ws_connect(ws_url, ssl=False) as ws:
                    logger.info("WebSocket connected. Streaming data...")

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.error("Data received timeout. Exiting.")
                            break

                        if msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                data_len = len(msg.data)
                                if data_len >= 100:
                                    logger.debug("Received binary data: %s", data_len)

                                if self.waiting_for_keyframe:
                                    if self._is_keyframe(msg.data):
                                        logger.info("Keyframe detected! Starting stream...")
                                        self.waiting_for_keyframe = False
                                        if audio_task is not None and not await self._wait_audio_ready(audio_task):
                                            audio_task.cancel()
                                            await asyncio.gather(audio_task, return_exceptions=True)
                                            audio_task = None
                                            self.audio_input = None
                                            await self.audio_ws.close()
                                            self.audio_ws = None
                                        self._start_ffmpeg()
                                        if audio_task is not None:
                                            # 音频时间线从第一帧视频写入时起算,两路起点对齐
                                            pump_task = asyncio.create_task(self._audio_pump())
                                    else:
                                        logger.debug("Skipping non-keyframe data...")
                                        continue
                                await asyncio.wait_for(self.process_write(msg.data), timeout=30.0)
                                self.last_progress = time.monotonic()
                            except asyncio.TimeoutError:
                                logger.error("Write data to process timeout.")
                                break
                            except BrokenPipeError as e:
                                logger.error("FFmpeg process terminated unexpectedly. %s", e)
                                break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocket connection closed with error {ws.exception()}")
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                            logger.info("WebSocket connection close(%s)", msg.type)
                            break
                        else:
                            logger.info(f"Unexpected WebSocket message type: {msg.type}")
            except Exception as e:
                logger.error(f"Streaming error", exc_info=True)
            finally:
                for task in (pump_task, audio_task):
                    if task is None:
                        continue
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.error("Audio task error", exc_info=True)
                if self.audio_ws is not None:
                    await self.audio_ws.close()
                    self.audio_ws = None
                self._stop_ffmpeg()
                logger.info("Stream finished")

    def _is_keyframe(self, data: bytes) -> bool:
        if self.video_codec == 'h264':
            i = 0
            while i < len(data) - 4:
                if (
                    data[i] == 0x00 and data[i + 1] == 0x00 and
                    ((data[i + 2] == 0x00 and data[i + 3] == 0x01) or data[i + 2] == 0x01)
                ):
                    nal_unit_type = (data[i + 3] & 0x1f) if data[i + 2] == 0x01 else (data[i + 4] & 0x1f)
                    return nal_unit_type == 5
                i += 1
            return False
        elif self.video_codec == 'hevc':
            i = 0
            while i < len(data) - 6:
                if (
                    data[i] == 0x00 and data[i + 1] == 0x00 and
                    ((data[i + 2] == 0x00 and data[i + 3] == 0x01) or data[i + 2] == 0x01)
                ):
                    nal_start = i + 3 if data[i + 2] == 0x01 else i + 4
                    nal_unit_type = (data[nal_start] >> 1) & 0x3f
                    if nal_unit_type in [16, 17, 18, 19, 20]:
                        return True
                i += 1
            return False
        return True

    async def process_write(self, data):
        process = self.process
        if not process or not process.stdin:
            # 音频那边已把 ffmpeg 收掉
            raise BrokenPipeError("FFmpeg not running")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._process_write, process.stdin, data)

    @staticmethod
    def _process_write(stdin, data):
        stdin.write(data)
        stdin.flush()


def main():
    parser = argparse.ArgumentParser(description="Bridge WebSocket video stream to RTSP")
    parser.add_argument("--base-url", default="", help="Base URL of the Miloco server")
    parser.add_argument("--username", default="admin", help="Login username")
    parser.add_argument("--password", default="", help="Login password (MD5)")
    parser.add_argument("--camera-id", default="", help="Camera ID to stream")
    parser.add_argument("--channel", default="", help="Camera channel")
    parser.add_argument("--video-codec", default="hevc", help="Input video codec (hevc or h264)")
    parser.add_argument("--rtsp-url", default="", help="Target RTSP URL")
    parser.add_argument("--no-audio", action="store_true", help="Bridge video only, skip audio")

    args = parser.parse_args()

    password = args.password or os.getenv("MILOCO_PASSWORD", "")
    if not password:
        logger.error("Password is required")
        return

    camera_id = args.camera_id or os.getenv("CAMERA_ID", "")
    if not camera_id:
        logger.error("Camera ID is required")
        return

    bridge = RTSPBridge(
        base_url=args.base_url or os.getenv("MILOCO_BASE_URL", "https://miloco:8000"),
        username=args.username or os.getenv("MILOCO_USERNAME", "admin"),
        password=password,
        camera_id=camera_id,
        rtsp_url=args.rtsp_url or os.getenv("RTSP_URL", "rtsp://0.0.0.0:8554/live"),
        video_codec=args.video_codec or os.getenv("VIDEO_CODEC", "hevc"),
        channel=args.channel or os.getenv("STREAM_CHANNEL", "0"),
        enable_audio=(
            not args.no_audio
            and os.getenv("ENABLE_AUDIO", "1").strip().lower()
            not in ("0", "false", "no", "off")
        ),
    )

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
