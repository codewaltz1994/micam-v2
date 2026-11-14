# Miloco 1.x 音频端点补丁镜像

## 为什么需要它

micam 的音频来自 Miloco 的独立端点 `/api/miot/ws/audio_stream`，而这个端点是
**Miloco 2.0** 才引入的。线上用的 `ghcr.nju.edu.cn/miiot/miloco:main` 是 1.x，
FastAPI 里根本没有这条路由，Starlette 对不存在的 WebSocket 路由直接拒绝，
micam 侧看到的就是：

```
WARNING - Audio stream unavailable, streaming video only: 403, message='Invalid response status'
```

注意这里的 403 **不是鉴权失败**（视频 WS 用同一个 cookie 能连上就证明了）。
本目录在 1.x 镜像上补出该端点，行为与 2.x 对齐。

## 补了什么

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 新增 `/api/miot/ws/audio_stream` | `audio_controller.py` → `/app/miloco_server/controller/` | cookie 鉴权复用 1.x 的 `verify_websocket_token`；先发 init 文本，再发裸 PCM |
| 注册路由 | `main.py` | 在 `include_router(miot_router)` 之后追加一行 |
| G.711 解码分支 | `miot_kit/miot/decoder.py` | 1.x 只给 OPUS 建解码器，G.711A/U 会抛 `AttributeError` 并被吞掉 → 永远没有数据 |

音频数据不做重新编码：直接复用 SDK 的 `register_decode_pcm_async`，它内部
（`AudioResampler(format="s16", layout="mono", rate=16000)`）已经解成
**16kHz / s16le / mono**。所以协议是：

```
server → client  {"type":"init","codec":"pcm","sampleRate":16000,"numberOfChannels":1}
server → client  <binary: PCM s16le 16kHz mono>
```

init 只在**首帧到达时**发送（与 2.x 一致）。相机没有音频帧时客户端会自己超时，
降级成纯视频，不会挂着一条永远没数据的音轨。

补丁脚本对锚点做精确匹配，上游代码一旦变化会**构建失败**，不会产出"看着正常但没声音"的镜像。

## 获取镜像

GitHub Actions（[docker-miloco-audio.yaml](../../.github/workflows/docker-miloco-audio.yaml)）在本目录有改动时
以及每周自动构建 amd64/arm64 多架构镜像：

```
ghcr.io/codewaltz1994/miloco-audio:main        # 国内：ghcr.nju.edu.cn/codewaltz1994/miloco-audio:main
```

根目录 `docker-compose.yml` 默认就用它，一般无需自己构建。

## 本地构建

```bash
cd deploy/miloco-audio

# 基础镜像默认 ghcr.nju.edu.cn/miiot/miloco:main；海外网络加 --build-arg BASE_IMAGE=ghcr.io/miiot/miloco:main
# 与服务器架构对应（NAS 常见 arm64；x86 用 amd64）
docker buildx build --platform linux/arm64 -t miloco:local-arm64 --load .
docker buildx build --platform linux/amd64 -t miloco:local-amd64 --load .

# 打成 tar 交付（tag 必须与 compose 中 MILOCO_IMAGE 一致）
docker tag miloco:local-arm64 miloco:local && docker save miloco:local | gzip -1 > ../../dist/miloco-linux-arm64.tar.gz
docker tag miloco:local-amd64 miloco:local && docker save miloco:local | gzip -1 > ../../dist/miloco-linux-amd64.tar.gz
```

## 部署

用 GHCR 镜像：

```bash
docker compose pull miloco micam1
docker compose up -d --force-recreate miloco micam1
```

用离线 tar 包：

```bash
# 1) 服务器上导入两个镜像（架构按机器实际选）
gunzip -c miloco-linux-arm64.tar.gz | docker load     # miloco:local
gunzip -c micam-linux-arm64.tar.gz  | docker load     # micam:local

# 2) .env 中设置 MILOCO_IMAGE=miloco:local、MICAM_IMAGE=micam:local，确认生效
docker compose config | grep image

# 3) 重建容器（miloco 数据卷 ./miloco 不动，登录态和配置都保留）
docker compose up -d --force-recreate
```

## 验证

服务端（把 log level 调到 info 才有这些日志）：

```bash
docker logs miloco 2>&1 | grep -i audio
#   Init MIoT Audio WS Manager (1.x backport)
#   Audio WebSocket connection request, admin, 1178721351.0
#   Start audio stream, 1178721351.0
#   audio decoder created, 1032 / 1027 / 1026
```

客户端（micam）：

```
INFO - Audio codec: pcm (16000 Hz, 1 ch)
INFO - Starting FFmpeg: ... -f s16le -ar 16000 -ac 1 -i pipe:<fd> ... -c:a aac ...
```

不再出现 `Audio stream unavailable ... 403`。用 `ffplay rtsp://<host>:8554/<stream>`
或 HA / Frigate 播放应能听到声音。

不需要真实相机即可自检补丁逻辑：

```bash
docker run --rm -e PYTHONPATH=/app \
  -v "$PWD/verify.py:/tmp/verify.py:ro" \
  --entrypoint python3 miloco:local-arm64 /tmp/verify.py
# OK: audio_controller 订阅/初始化/广播/退订逻辑通过
```

## 排障

| 现象 | 原因 |
| --- | --- |
| 仍报 403 | miloco 容器还是老镜像：`docker compose up -d --force-recreate miloco` 后确认 `docker inspect miloco --format '{{.Config.Image}}'` |
| 连上了但一直没声音 | 相机端关闭了拾音/录音；或码流是 OPUS（1.x 的 `_on_audio_callback` 只对 OPUS 建解码器，但 resampler 固定值是否匹配需另测）。先看 miloco 日志有没有 `audio decoder created` |
| micam 日志 `Unsupported audio codec 'opus'` | 服务端把原始 codec 原样下发（2.x 行为）。本补丁固定下发 `pcm`，出现这个说明连的不是本镜像 |
| 音频断了几十秒后画面重启 | 音频 WS 60s 无数据时 micam 会主动收掉 ffmpeg 交给容器重启，属预期降级路径 |

## 回滚

```bash
# .env 中设置 MILOCO_IMAGE=ghcr.nju.edu.cn/miiot/miloco:main
docker compose up -d --force-recreate miloco
```

回滚后 micam 会重新降级为纯视频（`ENABLE_AUDIO=0` 可直接关掉音频订阅）。
Miloco ≥ 2.0 不需要本补丁，但 2.x 的鉴权改成 `server.token`（bearer 或 `?token=`）、
端口与 `$MILOCO_HOME` 也变了，属于另一条迁移路径。
