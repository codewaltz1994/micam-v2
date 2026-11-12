#!/usr/bin/env python3
"""给 Miloco 1.x 的 miot_kit 补上 G.711 音频解码。

1.x 的 MIoTMediaDecoder._on_audio_callback 只给 AUDIO_OPUS 建解码器，
遇到 G.711A/G.711U 时 _audio_decoder 仍是 None，decode() 直接抛
AttributeError 并被 decoder 线程的 try/except 吞掉（日志里只是一行
"frame data handle error"），表现为「音频端点上永远没有数据」。

2.0 已经补上了这两个分支，这里按同样写法回填。找不到锚点就直接
退出非 0，让镜像构建失败，而不是产出一个静默无音频的镜像。
"""

import sys
from pathlib import Path

TARGET = Path("/app/miot_kit/miot/decoder.py")

OLD = '''    def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._audio_decoder:
            # Create audio decoder
            if frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
                self._audio_decoder = AudioCodecContext.create("opus", "r")
            self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            _LOGGER.info("audio decoder created, %s", frame_data.codec_id)
'''

NEW = '''    def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._audio_decoder:
            # Create audio decoder
            if frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
                self._audio_decoder = AudioCodecContext.create("opus", "r")
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711A:
                # G.711 是裸 8kHz/mono 的比特流，没有容器头，采样率和声道
                # 必须显式告诉解码器，否则 avcodec 会按默认值解出乱码。
                self._audio_decoder = AudioCodecContext.create("pcm_alaw", "r")
                self._audio_decoder.sample_rate = 8000
                self._audio_decoder.layout = "mono"
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711U:
                self._audio_decoder = AudioCodecContext.create("pcm_mulaw", "r")
                self._audio_decoder.sample_rate = 8000
                self._audio_decoder.layout = "mono"
            else:
                _LOGGER.error(
                    "unsupported audio codec: %s, skipping frame", frame_data.codec_id
                )
                return
            self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            _LOGGER.info("audio decoder created, %s", frame_data.codec_id)
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"[patch_decoder] target not found: {TARGET}", file=sys.stderr)
        return 1

    source = TARGET.read_text(encoding="utf-8")
    if "pcm_alaw" in source and "pcm_mulaw" in source:
        print("[patch_decoder] already patched, skip")
        return 0

    hits = source.count(OLD)
    if hits != 1:
        print(
            f"[patch_decoder] anchor matched {hits} times (expected 1); "
            "upstream layout changed, refusing to patch",
            file=sys.stderr,
        )
        return 1

    TARGET.write_text(source.replace(OLD, NEW), encoding="utf-8")
    print("[patch_decoder] G.711A / G.711U decoder branches added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
