# -*- coding: UTF-8 -*-
"""全局配置。

所有参数集中管理，方便调试和适配不同硬件。
"""

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_LEVEL = "INFO"           # DEBUG / INFO / WARN / ERROR / CRITICAL

# ---------------------------------------------------------------------------
# 按键 (GPIO) — 与 Portable_music 一致：GPIO13=选择, GPIO12=确认
# 手动拍照模式复用板载 GPIO13 作快门（由 camera_flow 在 manual 模式处理）
# ---------------------------------------------------------------------------
KEY_CAPTURE_GPIO = 13        # 选择键（原 S4/KEY_1）/ 手动拍照快门
KEY_FUNC_GPIO = 12           # 确认键（原 KEY_2）
KEY_FILTER_TIME = 50         # 硬件消抖(ms)，必须为 10 的倍数

# ---------------------------------------------------------------------------
# LCD (ST7789 240×240，LCM 接口1)
# ---------------------------------------------------------------------------
LCD_WIDTH = 240
LCD_HEIGHT = 240
LCD_CLK = 13000                     # 13MHz
LCD_DATA_LINE = 1                   # 数据线数（1/2）
LCD_LINE_NUM = 4                    # 线数（3/4）
LCD_TYPE = 0                        # 屏幕类型：0 - rgb；1 - fstn
LCD_DRAW_BUF_LINES = 40             # LVGL 显存行数（分块缓冲，降低 RAM 占用）

# ---------------------------------------------------------------------------
# 摄像头 (GC032A, SPI, 30 万像素)
# ---------------------------------------------------------------------------
CAM_MODEL = 0                # 0=GC032A
CAM_PREVIEW_LEVEL = 2        # 预览等级 1/2。EC600M 支持 1 或 2，等级越高 LCD 取景画面越流畅，消耗 RAM/CPU 越大
# 传感器出流分辨率：GC032A 原生 640x480。务必用全幅，让 LCD 预览与拍照存储取同一视野（照片不缺）
CAM_SENSOR_WIDTH = 640       # sensor 出流宽（camCapture cam_w）
CAM_SENSOR_HEIGHT = 480      # sensor 出流高（camCapture cam_h）
# 照片保存尺寸：240x240 全幅缩放保存，lv.img 零变换干净显示，无 OOM
CAM_CAPTURE_WIDTH = 240      # 保存照片宽
CAM_CAPTURE_HEIGHT = 240     # 保存照片高
CAM_PHOTO_DIR = "/usr"   # 照片存储目录：U:/ 盘符映射到 /usr/（uos API 须用 POSIX 路径，U:/ 前缀会 ENODEV）。用户指定照片直接放 U:/ 根下
CAM_MAX_PHOTOS = 100         # 最多保留照片数（循环存储）

# 定时拍摄（延时摄影）：间隔连拍
TIMED_CAPTURE_INTERVAL = 5   # 连拍间隔（秒），需 ≥ 单张拍照耗时
TIMED_CAPTURE_COUNT = 0      # 连拍张数；0=无限（直到手动返回停止）
# 定时拍照出图尺寸：GC032A sensor 原生 640×480，不做 LCD 同视口 240×240 缩放
TIMED_CAM_W = 640
TIMED_CAM_H = 480

# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
NET_TIMEOUT_PER_ROUND = 15   # 每次 checkNet 超时秒数
NET_MAX_RETRIES = 3          # 最大重试次数

# ---------------------------------------------------------------------------
# SIM 热插拔
# ---------------------------------------------------------------------------
SIM_HOTPLUG_TRIGGER_LEVEL = 1  # SIM 热插拔在位电平：1=高电平在位（绝大多数板子），0=低电平在位

# ---------------------------------------------------------------------------
# 照片上传（拍照完成后上传到云端）
# 使用 Gitee API 上传照片到公开仓库（免费，与音乐播放器同平台）
# 需要创建一个 Gitee 个人访问令牌（Token）用于鉴权
# ---------------------------------------------------------------------------
# --- Gitee 上传配置 ---
PHOTO_UPLOAD_GITEE_ENABLED = True        # 是否启用 Gitee 上传
GITEE_OWNER = "Arronchenzhihe"               # Gitee 用户名
GITEE_REPO = "my_photos"                  # 用于存照片的仓库名（需要先在 Gitee 创建）
GITEE_BRANCH = "master"                   # 上传到哪个分支
GITEE_PHOTO_PATH = "photos/"              # 仓库内照片路径前缀
GITEE_TIMED_PHOTO_PATH = "photos/timed/"  # 定时照片云端单独子目录（与普通拍照分开）
# Gitee 个人访问令牌：在 https://gitee.com/profile/personal_access_tokens 创建
# 需要勾选 "projects" 权限
GITEE_ACCESS_TOKEN = "2ba86a822c999050230458106ce5cecd"
# Gitee 原始照片链接模板（相册浏览与视觉分析使用）
GITEE_RAW_BASE = "https://raw.giteeusercontent.com/{owner}/{repo}/raw/{branch}/"
# Gitee API 上传地址
GITEE_API_CREATE = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"
# 上传超时（秒）
PHOTO_UPLOAD_TIMEOUT = 30  # 单张上传 HTTP 超时秒数（网络差时宁可 fail-fast 也别卡整个线程）

# ---------------------------------------------------------------------------
# 语音 AI（小智平台）--- 直接语音交互控制拍照
# ---------------------------------------------------------------------------
# --- 小智平台配置 ---
OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"
WSS_DEBUG = False                    # uwebsocket 调试输出（调试期开，稳定后关）

# --- 板卡信息（OTA 换参用）---
BOARD_TYPE = "kevin-box"             # 平台识别板卡类型，沿用官方
BOARD_NAME = "ec600m-camera"         # 自定义板卡名称
CARRIER = "CHINA UNICOM"             # SIM 卡运营商
APP_VERSION = "1.0.0"
APP_SHA256 = "c8a8ecb6d6fbcda682494d9675cd1ead240ecf38bdde75282a42365a0e396033"

# --- 音频/PA配置 ---
PA_GPIO = 9                         # 功放 NS4160 使能引脚（audio.Audio.set_pa 用）

# --- 网络重连 ---
RECONNECT_BACKOFF_MIN = 1           # 首次重连等待秒数
RECONNECT_BACKOFF_MAX = 30          # 指数退避上限秒数

# --- 语音按键 ---
VOICE_TALK_KEY_GPIO = 13            # 通话按键（GPIO13 = 原 S4/KEY_1，按住说话）
VOICE_FUNC_GPIO = 12                # 功能按键（GPIO12 = 原 KEY_2）

# ---------------------------------------------------------------------------
# AI 视觉分析（百度智能云 · 图像内容理解 image-understanding）
# 两步式异步接口：1) OAuth2 取 access_token；2) 提交任务拿 task_id；3) 轮询结果
# 需要在百度AI开放平台 https://ai.baidu.com/ 创建"图像识别"应用获取 API Key / Secret Key
# ---------------------------------------------------------------------------
AI_VISION_BAIDU_ENABLED = True      # 是否启用百度图像理解
BAIDU_AI_API_KEY = "e7ECBsV33rKJIKPIidOosTUS"   # 应用 API Key（替换）
BAIDU_AI_SECRET_KEY = "s5gtMLVKq28ghm2L9qU9Kgs9Jl4ObeKD"  # 应用 Secret Key（替换）
BAIDU_AI_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"   # 鉴权地址
BAIDU_AI_REQUEST_URL = "https://aip.baidubce.com/rest/2.0/image-classify/v1/image-understanding/request"   # 提交任务
BAIDU_AI_RESULT_URL = "https://aip.baidubce.com/rest/2.0/image-classify/v1/image-understanding/get-result"   # 结果查询
BAIDU_AI_TIMEOUT = 60               # 轮询结果超时(秒)
BAIDU_AI_POLL_INTERVAL = 1          # 轮询间隔(秒)
