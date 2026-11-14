# 🎦 Xiaomi Camera Streamer (micam-v2)

把小米摄像头的画面 **和声音** 通过 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 转成 RTSP 流，
推给 [go2rtc](https://github.com/AlexxIT/go2rtc)，供 Home Assistant / Frigate / Scrypted / VLC 等使用。

基于 [miiot/micam](https://github.com/miiot/micam)，在其之上增加：

| 特性 | 说明 |
| --- | --- |
| 🔊 音频 | 订阅 Miloco 的 `/api/miot/ws/audio_stream`，转成 AAC 与视频合流推送（上游只有视频） |
| 🧩 Miloco 1.x 音频补丁镜像 | 1.x 没有音频端点，`miloco-audio` 镜像补上该端点并修复 G.711 解码，见 [deploy/miloco-audio](deploy/miloco-audio/README.md) |
| 🩹 断线自愈 | 摄像头断网、ffmpeg 卡死后自动恢复，不再需要手动重启容器 |
| ⏱️ 音画稳定 | 音频按采样数计时 + 实时节拍写入，修复 ffmpeg 8 下视频每隔几分钟卡死断流的问题 |

```
小米摄像头 ──► Miloco ──WS(视频 hevc/h264)──► micam ──ffmpeg──► RTSP ──► go2rtc ──► HA / Frigate / ...
                     └─WS(音频 pcm 16k)───────┘        (AAC)
```


## 📦 镜像

| 镜像 | 说明 | 架构 |
| --- | --- | --- |
| `ghcr.io/codewaltz1994/micam-v2:main` | micam 转发服务 | amd64 / arm64 |
| `ghcr.io/codewaltz1994/miloco-audio:main` | Miloco 1.x + 音频端点补丁 | amd64 / arm64 |

> 国内网络可把 `ghcr.io` 换成 `ghcr.nju.edu.cn`（`docker-compose.yml` 默认已使用该镜像站）。
>
> 镜像由 GitHub Actions 自动构建：推送到 `main` 分支即发布，见 [.github/workflows](.github/workflows)。


## 🚀 快速开始

### 1. 部署

```shell
mkdir -p /opt/micam && cd /opt/micam
wget https://raw.githubusercontent.com/codewaltz1994/micam-v2/main/docker-compose.yml
wget -O .env https://raw.githubusercontent.com/codewaltz1994/micam-v2/main/.env.example
docker compose up -d miloco go2rtc
```

> 此时先只启动 Miloco 和 go2rtc，micam 需要先拿到 Miloco 密码与摄像头 ID 才能工作。

### 2. 配置 Miloco

> 🏠 也可以通过 [HAOS 加载项](https://gitee.com/hasscc/addons) 部署 Miloco，但它是原版镜像，**没有音频端点**，只能推视频。

1. 打开 `https://<服务器IP>:8000`（自签证书，必须用 **https** 并忽略证书错误）
2. 设置 Miloco 密码
3. 绑定小米账号，确认摄像头在线。摄像头离线？参考 [Xiaomi Miloco Q&A](https://github.com/XiaoMi/xiaomi-miloco/issues/56)
4. 获取摄像头 ID（DID）：F12 打开开发者工具 → 网络，在摄像头相关请求里找 `camera_id` / `did`
5. 计算密码 MD5（小写）：
   ```shell
   echo -n 'your_password' | md5sum      # macOS: md5 -s 'your_password'
   ```

### 3. 配置 go2rtc

1. 打开 `http://<服务器IP>:1984/config.html`
2. 添加空的流（micam 会主动推流进来）：
   ```yaml
   streams:
     cam_living:
     cam_door:
   ```
3. 保存并重启 go2rtc

### 4. 配置并启动 micam

编辑 `.env`：

```shell
MILOCO_PASSWORD=5f4dcc3b5aa765d61d8327deb882cf99   # 第 2 步算出的 md5
CAMERA_ID=1234567890                               # 摄像头 DID
RTSP_URL=rtsp://127.0.0.1:8554/cam_living          # 推到 go2rtc 的哪个流
```

```shell
docker compose up -d
docker compose logs -f micam1
```

正常日志：

```
INFO - Login successful. ...
INFO - Connecting to audio WebSocket: wss://miloco:8000/api/miot/ws/audio_stream?...
INFO - Connecting to WebSocket: wss://miloco:8000/api/miot/ws/video_stream?...
INFO - WebSocket connected. Streaming data...
INFO - Audio codec: pcm (16000 Hz, 1 ch)
INFO - Keyframe detected! Starting stream...
INFO - Starting FFmpeg: ... -c:v copy -c:a aac ...
```

### 5. 播放验证

```shell
ffprobe rtsp://<服务器IP>:8554/cam_living   # 应看到 Video: hevc + Audio: aac
ffplay  rtsp://<服务器IP>:8554/cam_living
```

或者在 go2rtc 网页里点开对应流的 `stream` 链接直接预览。


## 📷 多个摄像头

每个摄像头对应一个 micam 服务。`docker-compose.yml` 里已预留 `micam2`（默认 `scale: 0` 关闭）：

1. 把 `micam2` 的 `scale` 改为 `1`
2. 在 `.env` 中添加：
   ```shell
   CAMERA_2_ID=2345678901
   CAMERA_2_RTSP_URL=rtsp://127.0.0.1:8554/cam_door
   ```
3. 需要更多摄像头时，照抄 `micam2` 新增 `micam3`、`micam4` ……，并在 go2rtc 中加对应的流。


## ⚙️ 配置

> [!Note]
> 建议所有环境变量都写在 `.env` 中，用 `docker compose up -d` 生效，不要直接改 `docker-compose.yml`。

### micam

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MILOCO_PASSWORD` | **必填** | Miloco 网页密码的 md5（小写） |
| `CAMERA_ID` | **必填** | 摄像头 DID |
| `RTSP_URL` | **必填** | 推流地址，如 `rtsp://127.0.0.1:8554/cam_living`，8554 为 go2rtc 的 RTSP 端口 |
| `MILOCO_BASE_URL` | `https://miloco:8000` | Miloco 地址。HAOS 加载项部署时为 `https://homeassistant.local:28800` |
| `VIDEO_CODEC` | `hevc` | 摄像头视频编码，`hevc` 或 `h264` |
| `STREAM_CHANNEL` | `0` | 摄像头通道（多摄像头机型可选 `1`） |
| `ENABLE_AUDIO` | `1` | 是否推送音频，`0` 只推视频 |
| `MILOCO_HOST_IP` | `host-gateway` | 容器内 `miloco` 主机名解析到的 IP，Miloco 不在本机时改成其 IP |
| `MICAM_IMAGE` | `ghcr.nju.edu.cn/codewaltz1994/micam-v2:main` | micam 镜像 |

### Miloco

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MILOCO_PORT` | `8000` | 监听端口。若冲突，改这里并同步改 `MILOCO_BASE_URL` |
| `MILOCO_LOG_LEVEL` | `warning` | 日志级别，排查音频问题时改为 `info` |
| `MILOCO_IMAGE` | `ghcr.nju.edu.cn/codewaltz1994/miloco-audio:main` | Miloco 镜像 |

> ⚠️ 音频需要 **micam 和 miloco-audio 一起使用**。如果 Miloco 用的是原版镜像，micam 会打印
> `Audio stream unavailable ... 403`，并自动降级为只推视频。

### 音频说明

- 音频格式：Miloco 端先把 G.711 解成 16kHz / s16le / 单声道 PCM 再下发，micam 用 ffmpeg 编码为 AAC 48kHz 64kbps。
- 支持的输入：`pcm` / `g711a` / `g711u`。若上报 `opus`，日志会出现 `Unsupported audio codec`，此时只推视频。
- 摄像头在米家 App 里关闭了录音时，等待首个关键帧时会提示 `No audio from camera, streaming video only`，此时只推视频。


## 🩹 断线自愈机制

摄像头断网或 ffmpeg 卡死时，micam 会**主动退出**，由 `restart: always` 拉起重连，**请勿删除该重启策略**。

| 情况 | 表现 | 恢复方式 |
| --- | --- | --- |
| 摄像头断网 | 每 60s 出现一次 `Data received timeout. Exiting.` | 进程退出后容器重启并重连，摄像头恢复后音视频都会回来 |
| ffmpeg / RTSP 服务卡住 | `Write data to process timeout` | 当前连接结束，容器重启 |
| 事件循环被卡死 | `No video written to FFmpeg for 90s, exiting for restart` | 独立线程看门狗强制退出 |

ffmpeg 自身的报错会以 `FFmpeg:` 为前缀实时打印到日志里。


## 🛠️ 本地构建（可选）

不想用 GHCR 镜像，或需要修改代码时：

```shell
git clone https://github.com/codewaltz1994/micam-v2.git && cd micam-v2

docker build -t micam:local .
docker build -t miloco:local deploy/miloco-audio
# 海外网络：docker build --build-arg BASE_IMAGE=ghcr.io/miiot/miloco:main -t miloco:local deploy/miloco-audio

cat >> .env <<'EOF'
MICAM_IMAGE=micam:local
MILOCO_IMAGE=miloco:local
EOF
docker compose up -d
```

### 离线交付（服务器无法访问镜像仓库）

```shell
# 在构建机上，按服务器架构构建（uname -m：x86_64 → amd64，aarch64 → arm64）
docker buildx build --platform linux/amd64 -t micam:local --load .
docker buildx build --platform linux/amd64 -t miloco:local --load deploy/miloco-audio
docker save micam:local  | gzip > micam-linux-amd64.tar.gz
docker save miloco:local | gzip > miloco-linux-amd64.tar.gz

# 在服务器上
gunzip -c micam-linux-amd64.tar.gz  | docker load
gunzip -c miloco-linux-amd64.tar.gz | docker load
# .env 中设置 MICAM_IMAGE=micam:local、MILOCO_IMAGE=miloco:local
docker compose up -d --force-recreate
```

### 直接运行（不用 Docker）

需要 Python ≥ 3.8 和 ffmpeg：

```shell
uv run micam --base-url https://192.168.1.10:8000 --password <md5> \
  --camera-id 1234567890 --rtsp-url rtsp://192.168.1.10:8554/cam_living
# 其它参数：--video-codec h264  --channel 1  --no-audio  --username admin
```


## 🔄 更新

```shell
docker compose pull && docker compose up -d
```

Miloco 的登录状态与摄像头绑定保存在 `./miloco` 目录中，更新镜像不会丢失。


## ❓ 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `Audio stream unavailable ... 403` | Miloco 不是 miloco-audio 镜像。检查 `docker inspect miloco --format '{{.Config.Image}}'` |
| `Unsupported audio codec 'pcm'` | micam 镜像太旧，执行 `docker compose pull micam1` |
| 有画面没声音 | 先看 micam 日志是否有 `Audio codec: pcm`；再把 `MILOCO_LOG_LEVEL=info`，看 miloco 日志里是否有 `audio decoder created`；还要确认米家 App 里开启了录音 |
| 登录失败 | `MILOCO_PASSWORD` 要填 md5，不是明文 |
| `Connect call failed` | micam 连不上 Miloco，检查 `MILOCO_BASE_URL` / `MILOCO_HOST_IP` |
| 画面周期性断开 | 用 `docker compose logs micam1` 查看 `FFmpeg:` 开头的日志与断开时间点，并附上同一时间段的 miloco 日志提 issue |

更多 Miloco 音频补丁的原理、验证与回滚，见 [deploy/miloco-audio/README.md](deploy/miloco-audio/README.md)。


## 🧩 集成

- [Home Assistant: Generic Camera](https://www.home-assistant.io/integrations/generic)
- [Frigate NVR](https://github.com/blakeblackshear/frigate): [HAOS Add-on](https://github.com/blakeblackshear/frigate-hass-addons)
- [Scrypted](https://github.com/koush/scrypted): [HAOS Add-on](https://github.com/koush/scrypted/wiki/Installation:-Home-Assistant-OS)


## 🔗 相关链接

- [miiot/micam](https://github.com/miiot/micam)（上游项目）· [详细部署文档 & AI 问答](https://zread.ai/miiot/micam)
- [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco)
- [AlexxIT go2rtc](https://github.com/AlexxIT/go2rtc)

## 📄 License

[Apache-2.0](LICENSE)
