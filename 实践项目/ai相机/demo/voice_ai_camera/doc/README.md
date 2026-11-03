# 语音控制 AI 相机

> **版本 v2.3.0** · 2026-10-14
> 基于 EC600M-CN-LE (QuecPython) + GC032A(SPI) + ST7789(LCD) + NS4160(功放) 的智能语音控制相机。
>
> **手动拍照 + 小智 AI 语音拍照** + **定时连拍**（GPIO13 开/停 + Gitee 独立目录）+ **Gitee 照片云备份** + **百度 AI 视觉分析**（已并入 Photo Preview）。

---

## 1. 功能总览

| # | 功能 | 说明 | 状态 |
|---|------|------|------|
| 1 | 📷 **两种方式拍照** | **手动按键**（主菜单 Manual Photo + GPIO13 快门）/ **小智 AI 语音对话**（Voice AI 入口 + MCP tools） | ✅ |
| 2 | 🎥 **LCD 实时预览** | `CAM_PREVIEW_LEVEL=2`（EC600M 支持 1 或 2，等级越高取景越流畅，占用 RAM/CPU 越大）。**camPreview 不能与 camCapture 共存**（SPI 单例），只用 camCapture 自动推取景画面 | ✅ |
| 3 | ⏰ **定时连拍** | Interval Photo 入口 + GPIO13 短按开/停 + GPIO12 长按一键清理。**本地不存储**（上传成功立即删），云端独立目录 `photos/timed/`，持久化计数防重名 | ✅ |
| 4 | 🗂 **Gitee 照片云备份** | 普通帧 → `photos/`、定时帧 → `photos/timed/`，400→覆盖更新自动降级（HEAD 拿 sha → PUT），定时帧上传后立即删本地 | ✅ |
| 5 | 🔌 **SIM 卡热插拔** | `sim.setSimDet(1,1)` + `sim.setCallback` + `dataCall.setCallback`，插拔后由 dataCall 状态回调跟踪网络变化 | ✅ |
| 6 | 🧠 **百度 AI 视觉分析** | Photo Preview 页内 **🔍 ANALYZE 按钮**（底部第 4 个）：隐藏当前 lv.img 释放内存 → 百度图像理解 API → 结果文本 → 本地 TTS 播报。无需单独入口 | ✅ |
| 7 | 🔊 **本地 TTS 播报** | `audio.TTS(0).play(text)` + NS4160 功放手动使能（GPIO9 双脉冲协议）。vision.py 里独立初始化 Audio channel，任何模式可用 | ✅ |
| 8 | 🗂 **相册预览 + 删除 + 分析** | Photo Preview 入口：Local/Gitee 来源 → 底部 5 按钮（PREV / NEXT / DEL / 🔍 ANALYZE / BACK）。云端照片已下载到本地临时文件，分析时直接复用 | ✅ |
| 9 | 🧪 **稳定性** | 主循环每轮 `gc.collect()` + 定时帧后台线程 gc + upload finally del 大对象（content/b64_data/payload ~70KB/帧）+ 30s RAM watermark 日志 + PHOTO_UPLOAD_TIMEOUT=30s fail-fast | ✅ |

## 2. 主菜单功能 + GPIO 交互

| # | 菜单项 | GPIO13 短按 | GPIO12 短按 | GPIO12 长按 ≥ 1.5s |
|---|--------|-----------|-----------|-------------------|
| 1 | Voice AI | 选中 | 进入语音 AI 对话模式 | — |
| 2 | Manual Photo | 选中 | 进入（取景 + GPIO13 快门） | — |
| 3 | **Photo Preview** | 选中 | 相册（Local/Gitee + **删除 + 🔍 分析**） | — |
| 4 | **Interval Photo** | 选中 | 返回主菜单（需先 drain 队列） | 一键清理定时帧（本地+Gitee+计数）→ 留在待命 |

### Interval Photo 模式内 GPIO 行为

| 场景 | GPIO13 短按 | GPIO12 短按 | GPIO12 长按 |
|------|-----------|-----------|------------|
| 进入 interval（待命） | → **开始连拍** | → drain 队列 → 返回主菜单 | → 清理定时帧 → 留在待命 |
| 连拍中 | → **停止连拍**（drain 队列，留在待命） | → drain 队列 → 返回主菜单 | → 清理定时帧 → 继续连拍 |
| 已停止待命 | → **重新开始** | → drain 队列 → 返回主菜单 | → 清理 → 留在待命 |

**退出保证**：GPIO12 返回 / GPIO13 停止 / 拍满 TIMED_CAPTURE_COUNT → **先 drain_timed_queue_for_interval_exit(30000ms)** 确保最后一张定时帧上传完再退出，不丢帧。

### Photo Preview 浏览页 GPIO 行为

底部 5 按钮（LVGL 符号，x 坐标 240px 内均分）：

| 按钮 | LVGL 符号 | x_off | 功能 | RAM 安全 |
|------|----------|-------|------|---------|
| PREV | ◀ LEFT | 9 | 上一张 | — |
| NEXT | ▶ RIGHT | 56 | 下一张 | — |
| DEL | ✕ CLOSE | 103 | 删除（本地 + Gitee） | — |
| **🔍 ANALYZE** | SEARCH / AI（兜底） | 150 | **分析当前照片**：HIDDEN lv.img → gc.collect() → 后台线程调百度 AI → TTS 播报 | ✅ 先释放 lv.img 再启动分析（峰值 ~100KB RAM） |
| BACK | 🏠 HOME | 197 | 返回来源选择页 | — |

交互：GPIO13 短按循环选按钮，GPIO12 短按确认执行。分析完成后按 PREV/NEXT/BACK 任一按钮自动恢复显示（`update_album_image()` 清 HIDDEN 再 set_src）。

## 3. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                  Gitee 照片仓库                                  │
│             photos/  +  photos/timed/                             │
│           REST API: 上传 / 列表 / 删除                            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTPS（4G Cat.1）
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                  EC600M-CNLE 设备端（QuecPython）                  │
│                                                                   │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐│
│  │  voice_ai.py             │    │  vision.py                   ││
│  │  小智 AI WebSocket       │    │  百度图像理解 API + TTS      ││
│  │  录音 → STT → LLM → TTS  │    │  Photo Preview 页内 🔍 触发   ││
│  │  protocol.py (MCP tools) │    │  camera_srv.take_photo       ││
│  └──────────────────────────┘    └──────────────────────────────┘│
│                                                                   │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐│
│  │  camera_flow.py          │    │  gitee_manager.py            ││
│  │  各模式 camera 线程       │    │  Gitee REST API              ││
│  │  manual / voice          │    │  upload / delete / list      ││
│  │  interval (GPIO13 开停)  │    │  photos/ 与 photos/timed/   ││
│  └──────────────────────────┘    │  分流                        ││
│                                   └──────────────────────────────┘│
│  ┌──────────────────────────┐                                     │
│  │  _main.py（主循环+后台）  │    ┌──────────────────────────────┐│
│  │  普通/定时照片队列        │    │  net_manager.py             ││
│  │  定时帧后台上传线程       │    │  4G 联网 + SIM 热插拔       ││
│  │  RAM watermark + GC       │    │  网络状态回调               ││
│  └──────────────────────────┘    └──────────────────────────────┘│
│                                                                   │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐│
│  │  camera_srv.py           │    │  config.py                 ││
│  │  GC032A (SPI)            │    │  摄像头/网络/上传配置       ││
│  │  camCapture 单对象       │    │                            ││
│  │  640×480 / 240×240 双规格 │    └──────────────────────────────┘│
│  │  照片命名 + 持久化计数    │                                     │
│  └──────────────────────────┘    ┌──────────────────────────────┐│
│                                   │  photo_upload.py            ││
│  ┌──────────────────────────┐    │  本地读 → base64 → POST     ││
│  │  input.py                │    │  400 自动 HEAD→PUT 覆盖更新││
│  │  GPIO13 (选择/快门)      │    │  upload finally del 大对象   ││
│  │  GPIO12 (确认)           │    │  PHOTO_UPLOAD_TIMEOUT=30s   ││
│  └──────────────────────────┘    └──────────────────────────────┘│
│                                                                   │
│  audio.py (TTS + PA 使能) · lvgl_ui.py (界面) · logging.py       │
└─────────────────────────────────────────────────────────────────┘
```

## 4. 项目目录

```
code/voice_ai_camera/
├── doc/
│   ├── README.md                 # 本文档
│   ├── 项目方案文档.md            # 详细方案设计（旧）
│
├── test/
│   ├── test_mjpeg.py             # MJPEG 远程预览测试脚本（独立运行，不与主程序同时）
│   ├── test_gitee_upload.py      # Gitee 上传测试
│   ├── test_01_net_protocol.py   # 语音 AI：网络 + WebSocket + hello 握手
│   └── ...（其他测试脚本）
│
├── _main.py                      # 主循环 + 定时帧后台线程 + GPIO 中断 + GC + RAM watermark
├── config.py                     # 全部配置集中管理
├── camera_srv.py                 # 摄像头服务（camCapture + 拍照 + LCD 预览 + 持久化计数）
├── camera_flow.py                # 各模式 camera 线程（manual / voice / cloud / interval）
├── voice_ai.py                   # 小智 AI 语音通道：录音 → STT → LLM → TTS + PA 使能
├── vision.py                     # 百度图像理解 API + 本地 TTS 播报（Photo Preview 页内 🔍 触发）
├── audio.py                      # 音频管理：TTS 播放 + PA 功放使能（GPIO9 双脉冲）
├── gitee_manager.py              # Gitee REST API（upload/delete/list + photos/timed 子目录分流）
├── net_manager.py                # SIM 热插拔 + dataCall 网络回调 + 联网等待 + toggle_modem
├── photo_upload.py               # 照片上传（本地读 → base64 → POST → 400自动 HEAD→PUT 覆盖更新）
├── protocol.py                   # MCP tools 定义（camera.take_photo + camera.set_lcd_backlight）
├── input.py                      # 按键驱动（GPIO13=选择/快门, GPIO12=确认）
├── lvgl_ui.py                    # LVGL 界面（主菜单 5 项 + 相册预览/删除/分析 + 删除页 + Interval + UI 按键导航）
├── logging.py                    # 日志分级输出
└── __init__.py                   # 包初始化
```

## 5. 硬件准备

| 器件 | 说明 |
| --- | --- |
| EC600M-CNLE 开发板 | QuecPython 固件，4G Cat.1 |
| GC032A 摄像头 | SPI 接口，插入 J4 摄像头排针 |
| LCD（ST7789 240×240） | 插入 J3 LCD 接口（**必须**，否则 camCapture 无回调） |
| NS4160 功放 + 喇叭 | GPIO9 一线脉冲协议手动使能（2 次上升沿后保持高电平） |
| SIM 卡 + 4G 天线 | 注册网络 |

## 6. 部署步骤

### 6.1 Gitee 照片仓库配置（一次性）

1. Gitee 创建公开仓库（如 `my_photos`）
2. 设置 → 私人令牌 → 生成 token（需要 projects 权限）
3. 仓库保持两个目录结构：`photos/`（普通帧）+ `photos/timed/`（定时帧，自动创建）

### 6.2 修改 config.py 并烧录

关键配置项：

```python
# Gitee 照片仓库
GITEE_OWNER = "你的Gitee用户名"
GITEE_REPO = "my_photos"
GITEE_TOKEN = "你的私人令牌"

# 摄像头
CAM_PREVIEW_LEVEL = 2             # 预览等级 1/2，越高越流畅
TIMED_CAM_W = 640                 # 定时帧用 sensor 原生全幅（不走 240×240 LCD 适配）
TIMED_CAM_H = 480

# 定时连拍
TIMED_CAPTURE_INTERVAL = 5        # 帧间隔（秒）
TIMED_CAPTURE_COUNT = 0           # 0=无限，拍满自动停

# SIM 热插拔
SIM_HOTPLUG_TRIGGER_LEVEL = 1      # EC600M CN 系列默认高电平在位；低电平板子改 0
```

全量上传所有 `.py` 文件到设备 `/usr/` 目录。

### 6.3 运行

串口交互终端执行：
```python
>>> example.exec('/usr/_main.py')
```

正常启动日志：
```
[main] === 语音控制 AI 相机 ===
[ui] LCD + LVGL OK
[net] 联网成功: stage=3 state=1
[net] SIM 热插拔 + dataCall 网络状态监测已注册
[camera] camCapture open OK: 640x480 preview=2
```

### 6.4 烧录文件清单

| 文件 | 必须 | 说明 |
|------|------|------|
| `_main.py` | ✅ | 主循环 + 定时帧后台线程 + GC + RAM watermark |
| `config.py` | ✅ | 全部配置 |
| `camera_srv.py` | ✅ | 摄像头服务（**只 open camCapture，不 open camPreview**） |
| `camera_flow.py` | ✅ | 各模式 camera 线程 + interval GPIO13 开停 + GPIO12 长按清理 |
| `voice_ai.py` | ✅ | 小智 AI 语音通道 + PA 使能 |
| `vision.py` | ✅ | 百度视觉 + 本地 TTS |
| `audio.py` | ✅ | TTS 播放 + PA 使能 |
| `gitee_manager.py` | ✅ | Gitee REST API（含 sub_path 参数） |
| `net_manager.py` | ✅ | SIM 热插拔 + dataCall |
| `photo_upload.py` | ✅ | 照片上传 + 400 覆盖更新 + upload finally del 大对象 |
| `protocol.py` | ✅ | MCP tools（**inputSchema 顶层 type: object 必须有**，否则 LLM 报 400） |
| `lvgl_ui.py` | ✅ | LVGL 界面 |
| `input.py` | ✅ | GPIO 按键驱动 |
| `logging.py` | ✅ | 日志分级输出 |

> ⚠️ **camPreview 不能与 camCapture 共存**（SPI 单例），代码已回退为只 open camCapture，LCD 自动显示取景画面。**不要**再回去加 camPreview.open()。



## 8. Gitee 照片管理

### 目录分流

| 照片类型 | 本地路径 | Gitee 路径 | 命名前缀 | 本地清理 |
|---------|---------|-----------|---------|---------|
| 普通帧（手动/语音） | `/usr/photo_001.jpg` | `photos/photo_001.jpg` | `photo_` | 用户删 + 启动扫描清理旧（CAM_MAX_PHOTOS 上限） |
| **定时帧** | `/usr/timed_photo_001.jpg`（**上传后立即删**） | `photos/timed/timed_photo_001.jpg` | `timed_photo_` | 成功后立即删（**不占 Flash**） |

### 400 冲突自动处理

```
POST photos/timed/timed_photo_003.jpg → Gitee 400（文件已存在，多为重启后命名回退）
    → HEAD /photos/timed/timed_photo_003.jpg 拿 sha
    → PUT /photos/timed/timed_photo_003.jpg?sha={sha} 覆盖更新 → 200 成功
```

### 持久化计数

定时帧编号写 `/usr/_timed_count.txt`（`timed_photo_005 → 5`），下次 `_next_photo_name` 读文件 + 扫本地取 max，重启后不会重复编号撞 Gitee。

## 9. 定时连拍详解（Interval Photo）

| 参数 | config 键 | 默认 | 说明 |
|------|----------|------|------|
| 帧间隔（秒） | `TIMED_CAPTURE_INTERVAL` | 5 | 间隔需 ≥ 单张拍照耗时，否则因 BUSY 跳过触发 |
| 总张数 | `TIMED_CAPTURE_COUNT` | 0 | 0=无限（手动停）；>0 拍满自动停 |
| 摄像头分辨率 | `TIMED_CAM_W / TIMED_CAM_H` | 640 × 480 | sensor 原生全幅，不走 240×240 LCD 适配 |
| Gitee 子目录 | `GITEE_TIMED_PHOTO_PATH` | `photos/timed/` | 定时帧独立目录，云端与普通照片分离 |

### 一键清理（GPIO12 长按）

流程：
```
drain_timed_queue_for_interval_exit()  ← 确保后台线程把队列里的都传完
  → 扫 /usr/timed_photo_* 并 uos.unlink
  → gitee.delete_all_photos("photos/timed/") 批量删云端
  → uos.unlink("/usr/_timed_count.txt") 删持久化计数
  → _interval_taken = 0, _interval_last_ms = 0  重置
  → 留在待命状态（不退出 interval 模式，再按 GPIO13 可重新开始）
```

## 10. SIM 卡热插拔

| 事件 | 触发源 | 设备侧动作 |
|------|--------|-----------|
| SIM 拔出 | `sim.setCallback(args=2)` | 记录拔卡状态；网络断开后由 dataCall 回调继续报告状态 |
| 网络断开 | `dataCall.setCallback(args[1]=0)` | 记录网络断开状态，执行 3s 冷却防抖 |
| SIM 插回 | `sim.setCallback(args=1)` | 记录插卡状态，等待网络恢复 |
| 网络恢复 | `dataCall.setCallback(args[1]=1)` | 记录网络恢复状态 |

**必须配置**：`SIM_HOTPLUG_TRIGGER_LEVEL = 1`（EC600M CN 系列默认高电平在位）。如果实测板子是低电平在位，改成 0。

## 11. 稳定性策略

| 策略 | 位置 | 作用 |
|------|------|------|
| 主循环每轮 `gc.collect()` | `_main.py` main loop | RAM 不累积 |
| 定时帧后台线程每张处理完 gc | `_run_timed_upload_loop` | 上传线程不累积 |
| upload finally del 大对象 + gc | `photo_upload.py` | content(~16KB) / b64_data(~22KB) / payload → **上传完立即释放 ~70KB/帧**（关键！） |
| RAM watermark 每 30s 日志 | `_main.py` | `[main] RAM free=N bytes` 可视化泄漏趋势 |
| PHOTO_UPLOAD_TIMEOUT=30s | config.py | 网络差时 fail-fast，不阻塞线程太久 |
| 定时帧后台线程独立于主循环 | `_main.py _run_timed_upload_loop` | HTTP 阻塞不影响 camera 连拍节奏 |
| FIFO 队列（单槽→带锁列表） | `_pending_photo.append/pop(0) + _pending_lock` | 定时连拍不丢帧 |
| 后台线程启动标记独立 flag | `_timed_thread_started = True/False` | QuecPython `start_new_thread` 返回 None，不能用 is None 判 |

## 12. MCP 协议（语音 AI 调用相机）

### MCP Tools 列表（protocol.py mcp_tools_list）

| name | inputSchema | 说明 |
|------|-------------|------|
| `camera.take_photo` | `{"type":"object","properties":{}}` | 触发一张手动拍照 |
| `camera.set_lcd_backlight` | `{"type":"object","properties":{"level":{"type":"integer","minimum":0,"maximum":1}}}` | LCD 背光控制 |

> ⚠️ **inputSchema 顶层必须有 `"type":"object"`**，不能是空 dict `{}`，否则 LLM 报 400 错误。

### LLM 调用链路

```
小智 AI 语音 → STT("帮我拍张照") → LLM 拿到 tools 列表 → 决定调 camera.take_photo
    → MCP call → 设备端 take_photo() → 回调 → 入 FIFO 队列
    → 主循环队列 → Gitee 上传（启用时）
    → LLM 拿到回执 → TTS("已为你拍好照片")
```

### 常见 LLM 调用错误

| 错误 | 根因 | 修复 |
|------|------|------|
| `400 Invalid schema for function 'camera__take_photo': schema must be 'type: object', got 'type: null'` | protocol.py 里 `inputSchema: {}`（空 dict，没有 type 字段） | 改成 `{"type":"object","properties":{}}` |
| LLM 看到 camera.analyze_photo 工具但不想自动触发 | protocol.py mcp_tools_list 里删掉它 | 语音 AI 只调 take_photo，视觉分析独立走 Photo Preview 页内 🔍 按钮 |

## 13. 已知限制 / 踩坑

| 问题 | 状态 | 说明 |
|------|------|------|
| camPreview 与 camCapture 共存冲突 | **硬件限制（SPI 单例）** | 只能用 camCapture，LCD 自动显示取景画面。**不要**再回去加 camPreview.open() |
| QuecPython `start_new_thread` 返回 None | **语言限制** | 用独立 bool flag `_timed_thread_started` 判线程是否启动 |
| MicroPython `str(num)` 文件 IO 坑 | 已修 | 用 `"%d" % num` 替代 |
| 定时帧本地命名回退撞 Gitee | 已修 | `_timed_count.txt` 持久化跨重启 |
| voice 模式 AI 推理期间 LCD 黑屏 | 已修（GC） | RAM 累积导致的。如果还黑屏先查 RAM watermark 日志 |
| Gitee 400 多为文件已存在 | 正常降级 | HEAD 拿 sha → PUT 覆盖更新（多一次 HTTP 往返） |
| 摄像头初始化失败 | 检查 | 确认 GC032A 插入 J4；camCapture.open() 必须返回 0；LCD 必须已 init |
| EC600M 固件版本 | 检查 | QPY_OCPU_EC600MCN_LE_AI_FW.bin 或更旧版本 |

## 14. 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|---------|
| v1.x | — | 三模式拍照 + 阿里云 IoT + Gitee 上传 + Scan Code（GC032A 回调不触发，功能空壳） |
| **v1.6.0** | 2026-09-10 | 定时连拍初版 + 云端 photos/timed/ 分流 + 持久化计数 + 阿里云物模型属性细化（LocalPhotoCount/CloudPhotoCount/TimedPhotoCount/LastXxxPhotoName）+ 多源事件 photo_source + SIM 热插拔（sim.setDet + dataCall）+ MCP inputSchema 修复（type:object） |
| **v2.0.0** | 2026-09-11 | GC 稳定性（主循环+后台+upload finally del 大对象）+ PHOTO_UPLOAD_TIMEOUT=30s + GPIO13 开停定时连拍（toggle 语义，不再是单向启动）+ GPIO12 长按一键清理 + camPreview 回退（只 open camCapture）+ CAM_PREVIEW_LEVEL=2 + 阿里云服务全删（TakePhoto/GetStatus/ClearOldPhotos，拍照改走属性指令 TakePhotoCommand=1）+ 删除语音 AI 自动视觉分析（独立走 Analyze Photo 入口）+ SIM 拔出主动断云 + 本 README 从旧位置迁到 doc/README.md 整体重写 |
| **v2.1.0** | 2026-09-13 | **Analyze Photo 菜单项并入 Photo Preview**：主菜单 7→6 项；相册浏览页底部 4→5 按钮（PREV/NEXT/DEL/**🔍 ANALYZE**/BACK，SEARCH 符号 `getattr` 兜底 AI 文字）；ANALYZE 触发前 HIDDEN lv.img + gc.collect() 释放 ~16KB 防止分析峰值 ~100KB RAM OOM；复用已有 `_trigger_analyze`（遮罩+后台线程+TTS 播报）；更新 README（功能总览/主菜单/架构图/目录/踩坑表/版本历史） |
| **v2.2.0** | 2026-10-07 | **移除扫码（Scan Code）功能**：主菜单 6→5 项；删除 `scan_srv.py` 及全部扫码测试脚本；`camera_flow.py` / `lvgl_ui.py` / `_main.py` / `config.py` 清理扫码分支与 `SCAN_*` 配置；更新 README（功能总览/主菜单/目录/踩坑表/版本历史） |
| **v2.3.0** | 2026-10-14 | **移除阿里云 IoT**：删除 MQTT 客户端、云服务封装和物模型；移除 Cloud Control 菜单及状态/事件上报；保留 4G 网络、Gitee 上传/相册、定时拍摄、语音 AI 与百度视觉分析 |
