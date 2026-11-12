# Miloco 1.x 音频补丁镜像
#
# 上游 1.x 只有 /ws/video_stream，音频端点（/ws/audio_stream）是 2.0 才引入的，
# 所以 micam 连它拿到 403。这里在 1.x 镜像上补一个等价端点：复用 SDK 自带的
# register_decode_pcm_async 把 G.711 解成 16kHz/s16/mono 裸 PCM 再转发。
#
# 构建（两种架构分别构建，tag 与线上 compose 里的 image 对应）：
#   docker buildx build --platform linux/amd64 -t miloco:local-amd64 --load .
#   docker buildx build --platform linux/arm64 -t miloco:local-arm64 --load .
#
# 补丁脚本锚点不匹配会以非 0 退出，构建直接失败——不会产出"看着正常但没声音"的镜像。

FROM ghcr.nju.edu.cn/miiot/miloco:main

# 新增端点模块 + 两个就地补丁脚本
COPY audio_controller.py /app/miloco_server/controller/audio_controller.py
COPY patch_decoder.py /tmp/patch_decoder.py
COPY patch_main.py /tmp/patch_main.py

RUN python3 /tmp/patch_decoder.py \
 && python3 /tmp/patch_main.py \
 && rm -f /tmp/patch_decoder.py /tmp/patch_main.py \
 && find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && python3 -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ('/app/miloco_server/main.py', '/app/miot_kit/miot/decoder.py', '/app/miloco_server/controller/audio_controller.py')]" \
 && grep -q 'pcm_alaw' /app/miot_kit/miot/decoder.py \
 && grep -q 'miot_audio_router' /app/miloco_server/main.py

EXPOSE 8000
