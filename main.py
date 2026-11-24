# -*- coding: utf-8 -*-
"""
鱼跃快剪  v2
------------------------------------------------
- 打开视频 / 打开工程 / 自动保存工程（工程名 = 创建时刻，如 20261121213215.vsplit）
- 时间轴任意位置点击预览，添加分割点，选中片段可删除
- 两种导出模式：
  * 快速（无损）：切点自动对齐关键帧，全程 -c copy，不重编码
  * 精准（局部重编码）：切点精确到毫秒，只对切点到下一个关键帧之间的一小段重新编码
- 可为每个导出片段追加片头、片尾（在设置中指定，自动归一化后无损拼接）
- 快捷键可在设置里自定义；点击片段列表行自动播放该片段
"""

import os
import re
import sys
import json
import time
import shutil
import bisect
import base64
import hashlib
import tempfile
import datetime
import subprocess

from PySide6.QtCore import (
    Qt, QThread, QTimer, QUrl, QProcess, QRectF, QPointF, QSettings, QStandardPaths,
    QSize, QTime, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QSpinBox, QSystemTrayIcon, QTabWidget, QFormLayout,
    QProgressDialog, QPushButton, QSizePolicy, QStackedWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget, QAbstractItemView,
    QToolTip, QGroupBox, QGridLayout, QTimeEdit,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

try:
    import websocket as _websocket
except Exception:  # 打包缺失时仍可运行，只是 OBS 相关功能不可用
    _websocket = None


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

FFMPEG = None

APP_NAME = "鱼跃快剪"
LEGACY_APP_NAMES = ("视频快速分割工具", "鱼跃快剪")

PROJECT_SUFFIX = ".vsplit"

SHORTCUT_DEFS = [
    ("open_video", "打开视频", "Ctrl+O"),
    ("open_project", "打开工程", "Ctrl+Shift+O"),
    ("save_project", "保存工程", "Ctrl+S"),
    ("settings", "设置（快捷键 / 片头片尾）", "Ctrl+K"),
    ("add_split", "添加分割点", "S"),
    ("undo_split", "撤销上个切点", "Ctrl+Z"),
    ("clear_splits", "清空所有切点", "Ctrl+Shift+Z"),
    ("delete_segment", "删除选中片段", "Del"),
    ("mark_start_delete", "自动删除（从选中片段起每隔一段）", "Ctrl+Shift+D"),
    ("restore_segment", "恢复选中片段", "R"),
    ("export", "导出有效片段", "Ctrl+E"),
    ("play_pause", "播放 / 暂停", "Space"),
    ("back", "后退 5 秒", "Left"),
    ("forward", "前进 5 秒", "Right"),
]


# ---------------------------------------------------------------- 基础工具

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    bases = [app_dir(), os.path.join(app_dir(), "bin")]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        bases += [mei, os.path.join(mei, "bin")]
    for b in bases:
        p = os.path.join(b, exe)
        if os.path.isfile(p):
            return p
    w = shutil.which(name)
    if w:
        return w
    for p in (r"C:\ffmpeg\bin", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")):
        cand = os.path.join(p, exe)
        if os.path.isfile(cand):
            return cand
    return None


def setup_bins():
    global FFMPEG
    FFMPEG = find_tool("ffmpeg")
    return FFMPEG


# ---------------------------------------------------------------- OBS WebSocket（v5 协议）

def obs_request(host, port, password, request_type, request_data=None, timeout=5.0):
    """向 OBS WebSocket v5 发一条请求，返回 (成功?, 说明或 responseData)。"""
    if _websocket is None:
        return False, "缺少 websocket-client 组件，无法连接 OBS。"
    try:
        port = int(port)
    except Exception:
        return False, "端口号无效，请填写 1~65535 的数字。"
    host = (host or "127.0.0.1").strip() or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"   # IPv6 字面量需要方括号
    url = f"ws://{host}:{port}"
    ws = None
    try:
        ws = _websocket.create_connection(url, timeout=timeout)
        hello = json.loads(ws.recv())
        if hello.get("op") != 0:
            return False, "OBS WebSocket 握手异常：没有收到 Hello 报文，确认端口是 WebSocket 而不是 HTTP。"
        data = hello.get("d") or {}
        ident = {"rpcVersion": data.get("rpcVersion", 1), "eventSubscriptions": 0}
        auth = data.get("authentication") or {}
        if auth:
            pwd = password or ""
            if not pwd:
                return False, "该 OBS 开启了认证，但连接密码为空，请填写 WebSocket 密码。"
            secret = base64.b64encode(
                hashlib.sha256((pwd + auth.get("salt", "")).encode("utf-8")).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth.get("challenge", "")).encode("utf-8")).digest()).decode()
        ws.send(json.dumps({"op": 1, "d": ident}))
        raw = ws.recv()
        if not raw:
            return False, "OBS 关闭了连接：连接密码不正确（认证失败），请核对 OBS 里的 WebSocket 密码。"
        reply = json.loads(raw)
        if reply.get("op") != 2:
            return False, "OBS 拒绝了连接：认证失败，请核对连接密码。"
        ws.send(json.dumps({
            "op": 6,
            "d": {"requestType": request_type, "requestId": "vsplit-1",
                  "requestData": request_data or {}},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            if not raw:
                return False, "OBS 关闭了连接，请求没有得到响应（常见原因是认证信息不正确）。"
            msg = json.loads(raw)
            if msg.get("op") == 7:
                st = (msg.get("d") or {}).get("requestStatus") or {}
                if st.get("result"):
                    return True, (msg.get("d") or {}).get("responseData") or {}
                comment = st.get("comment") or f"错误码 {st.get('code')}"
                return False, f"OBS 返回失败：{comment}"
        return False, "等待 OBS 响应超时。"
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        low = detail.lower()
        if "handshake status 404" in low:
            return False, (f"连接 OBS 失败（{url}）：该端口返回了 HTTP 404，说明这个端口上是别的程序（例如优酷客户端、"
                           "某些本地 HTTP 服务）而不是 OBS 的 WebSocket 服务器。\n"
                           "解决办法：在 OBS「工具 → WebSocket 服务器设置」里把端口改成其它值（如 4456），"
                           "或退出占用该端口的程序，然后重新开启 OBS 的 WebSocket 服务器。")
        if password and ("closed" in low or "1006" in low or "reset" in low):
            return False, "OBS 主动断开了连接，通常是连接密码不正确（认证失败）。\n请核对 OBS → 工具 → WebSocket 服务器设置里的密码。"
        hint = "请确认 OBS 已启动、已开启 WebSocket 服务器，且 IP / 端口填写正确。"
        if password:
            hint += "若 OBS 开启了认证，还需确认密码正确。"
        return False, f"连接 OBS 失败（{url}）：{detail}\n{hint}"
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def obs_test(host, port, password, timeout=5.0):
    ok, res = obs_request(host, port, password, "GetVersion", timeout=timeout)
    if ok:
        return True, (f"连接成功：OBS {res.get('obsVersion', '?')}，"
                      f"WebSocket {res.get('obsWebSocketVersion', '?')}")
    return False, str(res)


class ObsTaskWorker(QThread):
    """后台执行 OBS 请求，避免卡住界面。"""
    done = Signal(bool, str)

    def __init__(self, host, port, password, request_type, request_data=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.password = password
        self.request_type = request_type
        self.request_data = request_data

    def run(self):
        try:
            if self.request_type == "GetVersion":
                ok, msg = obs_test(self.host, self.port, self.password)
            else:
                ok, payload = obs_request(self.host, self.port, self.password, self.request_type, self.request_data)
                if ok:
                    msg = "OK"
                else:
                    msg = str(payload)
            self.done.emit(bool(ok), str(msg))
        except Exception as exc:
            self.done.emit(False, f"调用失败：{exc}")


def probe_video(path):
    """只用 ffmpeg 读出时长、分辨率、帧率、编码、音频参数"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise RuntimeError("无法识别视频（ffmpeg 未能读出时长），请确认文件完整且是支持的格式。")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    start_m = re.search(r"Duration:.*?start:\s*(\d+(?:\.\d+)?)", text)
    start_time = float(start_m.group(1)) if start_m else 0.0
    br_m = re.search(r"Duration:.*?bitrate:\s*(\d+)\s*kb/s", text)
    bitrate = int(br_m.group(1)) * 1000 if br_m else 0

    vline = aline = None
    for line in text.splitlines():
        if "Stream #" in line and "Video:" in line and vline is None:
            vline = line
        if "Stream #" in line and "Audio:" in line and aline is None:
            aline = line

    width = height = 0
    fps = 0.0
    vcodec = pix_fmt = ""
    if vline:
        cm = re.search(r"Video:\s*([a-zA-Z0-9_]+)", vline)
        vcodec = cm.group(1) if cm else ""
        sm = re.search(r"(\d{2,5})x(\d{2,5})", vline)
        if sm:
            width, height = int(sm.group(1)), int(sm.group(2))
        fm = re.search(r"(\d+(?:\.\d+)?)\s*fps", vline)
        if fm:
            fps = float(fm.group(1))
        pm = re.search(r",\s*([a-z0-9]+)(?:\([^)]*\))?,\s*\d+x\d+", vline)
        pix_fmt = pm.group(1) if pm else "yuv420p"

    acodec = ""
    sample_rate = 48000
    channels = 2
    if aline:
        cm = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", aline)
        acodec = cm.group(1) if cm else ""
        srm = re.search(r"(\d+)\s*Hz", aline)
        if srm:
            sample_rate = int(srm.group(1))
        if "mono" in aline:
            channels = 1
        elif "stereo" in aline:
            channels = 2
        else:
            chm = re.search(r"(\d+)\s*channels", aline)
            if chm:
                channels = int(chm.group(1))

    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "duration": duration,
        "start_time": start_time,
        "width": width,
        "height": height,
        "fps": fps,
        "vcodec": vcodec,
        "pix_fmt": pix_fmt or "yuv420p",
        "acodec": acodec,
        "sample_rate": sample_rate,
        "channels": channels,
        "bitrate": bitrate,
        "size": size,
        "has_audio": bool(aline),
    }


def iter_keyframes(path, on_key, should_stop=None, start_time=0.0):
    """用 showinfo 只解码关键帧，逐个产出关键帧相对时间"""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info",
        "-skip_frame", "nokey", "-i", path,
        "-an", "-sn", "-vf", "showinfo", "-f", "null", "-",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    pat = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
    try:
        for line in proc.stderr:
            if should_stop and should_stop():
                break
            if "pts_time:" not in line or "iskey" not in line:
                continue
            m = pat.search(line)
            if not m:
                continue
            t = float(m.group(1)) - start_time
            if t >= 0:
                on_key(t)
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()


def fmt_time(t, with_ms=True):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h > 0:
        return (f"{h:d}:{m:02d}:{s:06.3f}" if with_ms else f"{h:d}:{m:02d}:{int(s):02d}")
    return (f"{m:02d}:{s:06.3f}" if with_ms else f"{m:02d}:{int(s):02d}")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def mp4_family(ext):
    return ext.lower() in (".mp4", ".mov", ".m4v")


def write_concat_list(files, path):
    lines = []
    for f in files:
        p = f.replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def seek_time(t):
    """流复制（-c copy）只能从关键帧起切，seek 目标必须精确且不能因四舍五入落到关键帧之前，
    否则 ffmpeg 会退回上一个关键帧，导致片段开头重复上一段画面。这里用 6 位小数并加 0.5ms 余量。"""
    return f"{max(0.0, float(t)) + 0.0005:.6f}"


def build_copy_cmd(src, start, end, out_path, use_mp4=True):
    dur = max(0.01, end - start)
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", seek_time(start), "-t", f"{dur:.6f}", "-i", src,
        "-map", "0:v:0?", "-map", "0:a?",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
    ]
    if use_mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


def build_encode_cmd(src, start, dur, out_path, info, use_mp4=True):
    codec = "libx265" if str(info.get("vcodec", "")).lower() in ("hevc", "h265") else "libx264"
    crf = "20" if codec == "libx265" else "18"
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, float(start)):.6f}", "-t", f"{max(0.01, dur):.6f}", "-i", src,
        "-map", "0:v:0?",
    ]
    if info.get("has_audio"):
        cmd += ["-map", "0:a:0?"]
    cmd += [
        "-c:v", codec, "-preset", "veryfast", "-crf", crf,
        "-pix_fmt", info.get("pix_fmt") or "yuv420p",
    ]
    if info.get("has_audio"):
        cmd += [
            "-c:a", "aac", "-b:a", "192k",
            "-ar", str(info.get("sample_rate") or 48000),
            "-ac", str(info.get("channels") or 2),
        ]
    if use_mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


def build_concat_cmd(list_file, out_path, use_mp4=True):
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", "-avoid_negative_ts", "make_zero",
    ]
    if use_mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


def build_normalize_cmd(aux_path, aux_info, target_info, out_path, use_mp4=True):
    """把片头/片尾归一化成与原视频同分辨率、同帧率、同编码，便于无损拼接"""
    w = target_info.get("width") or 1920
    h = target_info.get("height") or 1080
    fps = target_info.get("fps") or 0
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if fps and fps > 1:
        vf += f",fps={fps:.3f}"
    codec = "libx265" if str(target_info.get("vcodec", "")).lower() in ("hevc", "h265") else "libx264"
    crf = "20" if codec == "libx265" else "18"

    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", aux_path,
    ]
    need_silent = target_info.get("has_audio") and not aux_info.get("has_audio")
    if need_silent:
        ch = target_info.get("channels") or 2
        layout = "mono" if ch == 1 else ("stereo" if ch == 2 else f"{ch}c")
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={target_info.get('sample_rate') or 48000}:cl={layout}"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    elif target_info.get("has_audio"):
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:v:0", "-an"]

    cmd += ["-vf", vf, "-c:v", codec, "-preset", "veryfast", "-crf", crf,
            "-pix_fmt", target_info.get("pix_fmt") or "yuv420p"]
    if target_info.get("has_audio"):
        cmd += ["-c:a", "aac", "-b:a", "192k",
                "-ar", str(target_info.get("sample_rate") or 48000),
                "-ac", str(target_info.get("channels") or 2)]
    if use_mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


# ---------------------------------------------------------------- 赛事信息条

BANNER_FONT = "Microsoft YaHei"


def build_banner_image(event, width, height):
    """把赛事名称 / 地点 / 版权渲染成半透明字幕条（透明底，尺寸与视频一致）。"""
    name = str((event or {}).get("name") or "").strip()
    venue = str((event or {}).get("venue") or "").strip()
    credit = str((event or {}).get("copyright") or "").strip()
    if not (name or venue or credit):
        return None

    w = max(320, int(width or 1920))
    h = max(180, int(height or 1080))
    dot_offset = max(8, int(round(h * 0.026)))
    gap = max(4, int(round(h * 0.013)))
    pad_x = max(10, int(round(h * 0.032)))
    pad_y = max(8, int(round(h * 0.028)))
    accent_w = max(3, int(round(h * 0.009)))
    accent_gap = max(8, int(round(h * 0.018)))

    scale = 1.0
    while True:
        f_title = QFont(BANNER_FONT)
        f_title.setPixelSize(max(10, int(round(h * 0.060 * scale))))
        f_title.setWeight(QFont.Weight.Bold)
        f_venue = QFont(BANNER_FONT)
        f_venue.setPixelSize(max(9, int(round(h * 0.034 * scale))))
        f_venue.setWeight(QFont.Weight.DemiBold)
        f_credit = QFont(BANNER_FONT)
        f_credit.setPixelSize(max(8, int(round(h * 0.024 * scale))))

        rows = []
        if name:
            rows.append({"text": name, "font": f_title, "color": QColor("#FFFFFF"),
                         "shadow": QColor(0, 0, 0, 150), "indent": 0})
        if venue:
            rows.append({"text": venue, "font": f_venue, "color": QColor("#EDF1F7"),
                         "shadow": QColor(0, 0, 0, 120), "indent": dot_offset})
        if credit:
            rows.append({"text": credit, "font": f_credit, "color": QColor("#B6BCC7"),
                         "shadow": None, "indent": 0})

        heights = [QFontMetrics(r["font"]).height() for r in rows]
        text_h = sum(heights) + gap * (len(rows) - 1)
        text_w = 0
        for r in rows:
            text_w = max(text_w, QFontMetrics(r["font"]).horizontalAdvance(r["text"]) + r["indent"])
        block_w = pad_x * 2 + accent_w + accent_gap + text_w
        block_h = pad_y * 2 + text_h
        if block_w <= w * 0.92 or scale <= 0.45:
            break
        scale *= 0.90

    left = int(round((w - block_w) / 2.0))
    top = int(round((h - block_h) / 2.0))
    if not event.get("pos_auto", True):
        # 用户指定“文字左上角”的像素坐标，背景板按内边距向左上扩展
        text_x = max(0, int(event.get("pos_x") or 0))
        text_y = max(0, int(event.get("pos_y") or 0))
        left = text_x - (pad_x + accent_w + accent_gap)
        top = text_y - pad_y

    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    radius = max(4, int(round(h * 0.018)))
    bg = QPainterPath()
    bg.addRoundedRect(float(left), float(top), float(block_w), float(block_h),
                      float(radius), float(radius))
    grad = QLinearGradient(0.0, float(top), 0.0, float(top + block_h))
    grad.setColorAt(0.0, QColor(8, 10, 14, 174))
    grad.setColorAt(1.0, QColor(8, 10, 14, 136))
    p.fillPath(bg, grad)
    p.setPen(QPen(QColor(255, 255, 255, 42), max(1, int(round(h * 0.0015)))))
    p.drawPath(bg)

    bar_x = left + pad_x
    bar_y = top + pad_y
    bar_h = block_h - pad_y * 2
    bar = QPainterPath()
    bar.addRoundedRect(float(bar_x), float(bar_y), float(accent_w), float(bar_h),
                       float(accent_w / 2), float(accent_w / 2))
    bar_grad = QLinearGradient(0.0, float(bar_y), 0.0, float(bar_y + bar_h))
    bar_grad.setColorAt(0.0, QColor("#FFA43C"))
    bar_grad.setColorAt(1.0, QColor("#F0741B"))
    p.fillPath(bar, bar_grad)

    text_x = bar_x + accent_w + accent_gap
    y = top + pad_y
    for r, lh in zip(rows, heights):
        rect = QRectF(float(text_x + r["indent"]), float(y), float(max(10, text_w - r["indent"])), float(lh))
        p.setFont(r["font"])
        if r["shadow"] is not None:
            p.setPen(r["shadow"])
            p.drawText(rect.translated(0, max(1, int(round(h * 0.0022)))),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), r["text"])
        p.setPen(r["color"])
        p.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), r["text"])
        if r["indent"]:
            dot_r = max(3, int(round(h * 0.0072)))
            cy = y + lh / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F4913A"))
            p.drawEllipse(QPointF(float(text_x + dot_r), float(cy)), float(dot_r), float(dot_r))
            p.setBrush(Qt.BrushStyle.NoBrush)
        y += lh + gap
    p.end()
    return img


def render_banner_preview(event, width=640, height=360):
    """给设置界面用的预览：把字幕条叠在一块深色渐变上。"""
    img = build_banner_image(event, width, height)
    if img is None:
        return None
    base = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    g = QLinearGradient(0.0, 0.0, float(width), float(height))
    g.setColorAt(0.0, QColor("#2a3140"))
    g.setColorAt(0.55, QColor("#1d232e"))
    g.setColorAt(1.0, QColor("#3a2c22"))
    p = QPainter(base)
    p.fillRect(0, 0, width, height, g)
    p.drawImage(0, 0, img)
    p.end()
    return base


def build_overlay_head_cmd(src, start, dur, out_path, info, banner, overlay_sec, use_mp4=True):
    """重编码片段开头一小段并把赛事信息烧进去（overlay_sec 之后隐藏字幕条）。"""
    codec = "libx265" if str(info.get("vcodec", "")).lower() in ("hevc", "h265") else "libx264"
    crf = "20" if codec == "libx265" else "18"
    fps = info.get("fps") or 30
    show = max(0.05, float(overlay_sec))
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, float(start)):.6f}", "-t", f"{max(0.01, dur):.6f}", "-i", src,
        "-loop", "1", "-framerate", f"{max(1, int(round(float(fps))))}", "-i", banner,
        "-filter_complex",
        f"[0:v:0][1:v]overlay=0:0:enable='lte(t,{show:.3f})'[v]",
        "-map", "[v]",
    ]
    if info.get("has_audio"):
        cmd += ["-map", "0:a:0?"]
    cmd += [
        "-c:v", codec, "-preset", "veryfast", "-crf", crf,
        "-pix_fmt", info.get("pix_fmt") or "yuv420p",
    ]
    if info.get("has_audio"):
        cmd += [
            "-c:a", "aac", "-b:a", "192k",
            "-ar", str(info.get("sample_rate") or 48000),
            "-ac", str(info.get("channels") or 2),
        ]
    cmd += ["-shortest"]
    if use_mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]
    return cmd


# ---------------------------------------------------------------- 时间轴控件

class TimelineWidget(QWidget):
    seekRequested = Signal(float)
    segmentSelected = Signal(int)
    segmentToggled = Signal(int)

    PAD = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.markers = []
        self.segments = []
        self.position = 0.0
        self.selected_index = -1
        self._dragging = False
        self._hover_x = None
        self.setMouseTracking(True)
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_duration(self, d):
        self.duration = max(0.0, d)
        self.update()

    def set_markers(self, markers):
        self.markers = list(markers)
        self.update()

    def set_segments(self, segments):
        self.segments = segments
        self.update()

    def set_position(self, t):
        self.position = max(0.0, min(t, self.duration)) if self.duration else 0.0
        self.update()

    def set_selected(self, idx):
        self.selected_index = idx
        self.update()

    def _x(self, t):
        w = self.width() - 2 * self.PAD
        if self.duration <= 0 or w <= 0:
            return self.PAD
        return self.PAD + (min(max(t, 0.0), self.duration) / self.duration) * w

    def _t(self, x):
        w = self.width() - 2 * self.PAD
        if w <= 0 or self.duration <= 0:
            return 0.0
        ratio = (x - self.PAD) / w
        return max(0.0, min(1.0, ratio)) * self.duration

    def _segment_at(self, t):
        for i, seg in enumerate(self.segments):
            if seg["start"] <= t <= seg["end"]:
                return i
        return -1

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        p.fillRect(self.rect(), QColor("#14151a"))
        ruler_h = 20
        band_top = ruler_h + 8
        band_bottom = h - 10

        if self.duration <= 0:
            p.setPen(QColor("#5a5f6a"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "未加载视频")
            return

        steps = [0.05, 0.1, 0.2, 0.5, 1, 2, 3, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]
        px_per_sec = (w - 2 * self.PAD) / self.duration if self.duration else 0
        step = next((s for s in steps if s * px_per_sec >= 68), steps[-1])
        p.setPen(QPen(QColor("#2b2f38"), 1))
        p.drawLine(QPointF(self.PAD, band_top - 2), QPointF(w - self.PAD, band_top - 2))
        p.setFont(QFont("Segoe UI", 8))
        t = 0.0
        while t <= self.duration + 1e-6:
            x = self._x(t)
            p.setPen(QPen(QColor("#3a3f4a"), 1))
            p.drawLine(QPointF(x, 4), QPointF(x, band_top - 2))
            p.setPen(QColor("#8b919c"))
            label = fmt_time(t, with_ms=False)
            lx = max(0.0, min(x - 40, w - 82.0))
            p.drawText(QRectF(lx, 4, 82, 14), Qt.AlignmentFlag.AlignCenter, label)
            t += step

        band = QRectF(0, band_top, w, band_bottom - band_top)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0e0f13"))
        p.drawRoundedRect(band, 3, 3)

        band_h = band_bottom - band_top - 2
        for i, seg in enumerate(self.segments):
            x0, x1 = self._x(seg["start"]), self._x(seg["end"])
            r = QRectF(x0 + 1, band_top + 1, max(0.5, x1 - x0 - 2), band_h)
            if seg.get("deleted"):
                p.setBrush(QColor("#4a2323"))
                p.setPen(QPen(QColor("#7d3a3a"), 1))
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QPen(QColor(220, 110, 110, 70), 1))
                x = r.left() - r.height()
                while x < r.right():
                    p.drawLine(QPointF(x, r.bottom()), QPointF(x + r.height(), r.top()))
                    x += 9
                if r.width() > 46:
                    p.setPen(QColor("#d98a8a"))
                    p.setFont(QFont("Microsoft YaHei UI", 8))
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, "已删除")
            else:
                selected = (i == self.selected_index)
                p.setBrush(QColor("#1f6f52" if not selected else "#2a8f68"))
                p.setPen(QPen(QColor("#48b184" if selected else "#2f8f66"), 1))
                p.drawRoundedRect(r, 3, 3)
                if r.width() > 52:
                    p.setPen(QColor("#d8f3e7"))
                    p.setFont(QFont("Microsoft YaHei UI", 8))
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, f"片段 {i + 1}")

        for m in self.markers:
            x = self._x(m)
            p.setPen(QPen(QColor("#ffc857"), 1))
            p.drawLine(QPointF(x, band_top - 4), QPointF(x, h - 4))
            path = QPainterPath()
            path.moveTo(x, band_top - 2)
            path.lineTo(x - 4.5, band_top - 9)
            path.lineTo(x + 4.5, band_top - 9)
            path.closeSubpath()
            p.setBrush(QColor("#ffc857"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)

        x = self._x(self.position)
        p.setPen(QPen(QColor("#4aa3ff"), 2))
        p.drawLine(QPointF(x, band_top - 12), QPointF(x, h - 3))
        p.setBrush(QColor("#4aa3ff"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(x, h - 3), 3.5, 3.5)

        if self._hover_x is not None and not self._dragging:
            x = max(self.PAD, min(self._hover_x, w - self.PAD))
            p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, band_top), QPointF(x, h - 4))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration > 0:
            self._dragging = True
            t = self._t(event.position().x())
            self.seekRequested.emit(t)
            idx = self._segment_at(t)
            if idx >= 0:
                self.segmentSelected.emit(idx)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton and self.duration > 0:
            idx = self._segment_at(self._t(event.position().x()))
            if idx >= 0:
                self.segmentSelected.emit(idx)

    def mouseMoveEvent(self, event):
        if self.duration <= 0:
            return
        x = event.position().x()
        self._hover_x = x
        t = self._t(x)
        if self._dragging:
            self.seekRequested.emit(t)
        QToolTip.showText(event.globalPosition().toPoint(), fmt_time(t), self)
        self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.duration > 0 and event.button() == Qt.MouseButton.LeftButton:
            idx = self._segment_at(self._t(event.position().x()))
            if idx >= 0:
                self.segmentToggled.emit(idx)

    def leaveEvent(self, event):
        self._hover_x = None
        QToolTip.hideText()
        self.update()


# ---------------------------------------------------------------- 后台线程

class KeyframeWorker(QThread):
    progress = Signal(float, int)
    finished_keys = Signal(list)

    def __init__(self, path, duration, start_time=0.0):
        super().__init__()
        self.path = path
        self.duration = duration
        self.start_time = start_time
        self._stop = False

    def run(self):
        keys = []
        last_emit = time.time()

        def on_key(t):
            nonlocal last_emit
            keys.append(t)
            now = time.time()
            if now - last_emit > 0.15:
                last_emit = now
                frac = min(1.0, t / self.duration) if self.duration else 0.0
                self.progress.emit(frac, len(keys))

        try:
            iter_keyframes(self.path, on_key, should_stop=lambda: self._stop,
                           start_time=self.start_time)
        except Exception:
            pass
        if not self._stop:
            self.progress.emit(1.0, len(keys))
            self.finished_keys.emit(keys)

    def stop(self):
        self._stop = True


class ExportWorker(QThread):
    phase = Signal(str)
    seg_started = Signal(int, int, float)
    seg_progress = Signal(int, float)
    overall = Signal(float)
    done = Signal(list)
    failed = Signal(str, list)

    def __init__(self, src, segments, out_dir, stem, ext, info, keyframes,
                 precise, intro, outro, banner="", title_seconds=0.0, cover=True, parent=None):
        super().__init__(parent)
        self.src = src
        self.segments = segments
        self.out_dir = out_dir
        self.stem = stem
        self.ext = ext
        self.info = info or {}
        self.keyframes = keyframes or []
        self.precise = precise
        self.intro = intro or ""
        self.outro = outro or ""
        self.banner = banner or ""
        self.title_seconds = float(title_seconds or 0.0)
        self.make_cover = bool(cover)
        self.cover_files = []
        self._title_pending = None   # 片头已吃掉部分字幕时长后，正文还剩多少秒
        self.use_mp4 = mp4_family(ext)
        self.inter_ext = ".mp4" if self.use_mp4 else ".mkv"
        self._cancel = False
        self._proc = None
        self._cur_out = ""
        self._seg_idx = 0
        self.outputs = []
        self._work = ""

    # -------- 控制
    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _cleanup_partial(self):
        if self._cur_out and os.path.exists(self._cur_out):
            try:
                os.remove(self._cur_out)
            except OSError:
                pass

    # -------- 执行一条 ffmpeg
    def _run(self, cmd, duration, on_frac=None):
        """返回 (rc, stderr文本)"""
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            return -1, f"启动 ffmpeg 失败：{exc}"
        for line in self._proc.stdout:
            if self._cancel:
                break
            if not line.startswith("out_time_us="):
                continue
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if on_frac:
                frac = max(0.0, min(1.0, (us / 1e6) / max(0.01, duration)))
                on_frac(frac)
        try:
            self._proc.stdout.close()
        except Exception:
            pass
        err = (self._proc.stderr.read() or "").strip()
        rc = self._proc.wait()
        return rc, err

    def _emit(self, lo, hi, frac):
        self.seg_progress.emit(self._seg_idx, lo + (hi - lo) * max(0.0, min(1.0, frac)))

    # -------- 主流程
    def run(self):
        self._work = tempfile.mkdtemp(prefix="vsplit_")
        try:
            norm_intro = norm_outro = None
            intro_dur = 0.0
            prep_w = 0.0
            if self.intro and os.path.isfile(self.intro):
                self.phase.emit("正在归一化片头…")
                norm_intro, intro_dur = self._normalize_aux(self.intro, "intro")
                prep_w += 1
                if norm_intro and self.banner:
                    norm_intro, used, intro_dur = self._burn_intro_title(norm_intro, intro_dur)
                    self._title_pending = max(0.0, self.title_seconds - used)
            if self.outro and os.path.isfile(self.outro):
                self.phase.emit("正在归一化片尾…")
                norm_outro, _d = self._normalize_aux(self.outro, "outro")
                prep_w += 1
            if self.intro and not os.path.isfile(self.intro):
                self.phase.emit(f"片头文件不存在，已忽略：{self.intro}")
            if self.outro and not os.path.isfile(self.outro):
                self.phase.emit(f"片尾文件不存在，已忽略：{self.outro}")

            seg_durs = [max(0.01, s["end"] - s["start"]) for s in self.segments]
            total_w = sum(seg_durs) + prep_w * 0.5
            done_w = 0.0

            for i, seg in enumerate(self.segments):
                if self._cancel:
                    self._cleanup_partial()
                    self.failed.emit("已取消导出", self.outputs)
                    return
                self._seg_idx = i
                self.seg_started.emit(i, len(self.segments), seg_durs[i])
                self.phase.emit(f"正在导出第 {i + 1}/{len(self.segments)} 个片段")
                out_path = os.path.join(self.out_dir, f"{self.stem}_片段{i + 1}{self.ext}")
                self._cur_out = out_path

                ok, err = self._build_segment(seg, i, norm_intro, norm_outro, out_path)
                if self._cancel:
                    self._cleanup_partial()
                    self.failed.emit("已取消导出", self.outputs)
                    return
                if not ok:
                    self._cleanup_partial()
                    self.failed.emit(f"第 {i + 1} 段导出失败：\n{err or '未知错误'}", self.outputs)
                    return
                if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    self._cleanup_partial()
                    self.failed.emit(f"第 {i + 1} 段导出失败：输出文件为空", self.outputs)
                    return

                self.outputs.append(out_path)
                self._write_cover(out_path, seg["start"])
                done_w += seg_durs[i]
                self.seg_progress.emit(i, 1.0)
                self.overall.emit(min(1.0, (done_w + prep_w * 0.5) / total_w))

            self.done.emit(self.outputs)
        finally:
            shutil.rmtree(self._work, ignore_errors=True)

    def _normalize_aux(self, path, tag):
        out = os.path.join(self._work, f"{tag}_norm{self.inter_ext}")
        try:
            aux_info = probe_video(path)
        except Exception:
            return None, 0.0
        dur = float(aux_info.get("duration", 0) or 1)
        cmd = build_normalize_cmd(path, aux_info, self.info, out, self.use_mp4)
        self._run(cmd, dur)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out, dur
        return None, 0.0

    def _burn_intro_title(self, path, dur):
        """把赛事信息烧进归一化片头的开头，保证视频首帧（文件封面）就带信息。"""
        if not (self.banner and os.path.isfile(self.banner) and self.title_seconds > 0.05):
            return path, 0.0, 0.0
        secs = min(self.title_seconds, max(0.1, dur - 0.02))
        out = os.path.join(self._work, "intro_title" + self.inter_ext)
        self.phase.emit("正在给片头叠加赛事信息（视频封面）…")
        cmd = build_overlay_head_cmd(path, 0.0, max(0.1, dur), out, self.info,
                                     self.banner, secs, self.use_mp4)
        ok, _err = self._run(cmd, max(0.1, dur))
        if ok == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out, secs, float(dur)
        return path, 0.0, float(dur)

    def _write_cover(self, out_path, t):
        """用原片该时间点的画面 + 赛事信息字幕条，生成同名封面图（不影响视频本身）。"""
        if not (self.make_cover and self.banner and os.path.isfile(self.banner)):
            return ""
        base, _ = os.path.splitext(out_path)
        cover = base + "_封面.jpg"
        cmd = [
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, float(t)):.3f}", "-i", self.src,
            "-i", self.banner,
            "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
            "-frames:v", "1", "-q:v", "2", cover,
        ]
        ok, _err = self._run(cmd, 1.0)
        if ok == 0 and os.path.exists(cover) and os.path.getsize(cover) > 0:
            self.cover_files.append(cover)
            return cover
        return ""

    def _build_segment(self, seg, idx, norm_intro, norm_outro, final_path):
        s, e = seg["start"], seg["end"]
        dur = max(0.01, e - s)
        core_path = final_path if not (norm_intro or norm_outro) else os.path.join(
            self._work, f"core_{idx}{self.inter_ext}")
        core_hi = 1.0 if not (norm_intro or norm_outro) else 0.74

        if self.banner and self.title_seconds > 0 and os.path.isfile(self.banner):
            secs = self.title_seconds if self._title_pending is None else self._title_pending
            if secs > 0.05:
                ok, err = self._build_with_title(s, e, dur, core_path, 0.0, core_hi, secs)
            elif self.precise or not self._on_keyframe(s):
                ok, err = self._build_precise(s, e, dur, core_path, 0.0, core_hi)
            else:
                self._cur_out = core_path
                ok, err = self._step_copy(s, e, dur, core_path, 0.0, core_hi)
        elif self.precise or not self._on_keyframe(s):
            ok, err = self._build_precise(s, e, dur, core_path, 0.0, core_hi)
        else:
            self._cur_out = core_path
            ok, err = self._step_copy(s, e, dur, core_path, 0.0, core_hi)
        if not ok:
            return False, err

        if not (norm_intro or norm_outro):
            return True, ""

        parts = []
        if norm_intro:
            parts.append(norm_intro)
        parts.append(core_path)
        if norm_outro:
            parts.append(norm_outro)
        list_file = os.path.join(self._work, f"list_{idx}.txt")
        write_concat_list(parts, list_file)
        self._cur_out = final_path
        cmd = build_concat_cmd(list_file, final_path, self.use_mp4)
        ok, err = self._run(cmd, dur, lambda f: self._emit(core_hi, 1.0, f))
        return ok == 0, err

    def _keyframe_after(self, t):
        if not self.keyframes:
            return None
        i = bisect.bisect_right(self.keyframes, t + 0.0005)
        return self.keyframes[i] if i < len(self.keyframes) else None

    def _on_keyframe(self, t):
        if not self.keyframes:
            return False
        i = bisect.bisect_left(self.keyframes, t)
        for j in (i - 1, i):
            if 0 <= j < len(self.keyframes) and abs(self.keyframes[j] - t) <= 0.03:
                return True
        return False

    def _build_precise(self, s, e, dur, core_path, lo, hi):
        """局部重编码：仅在切点不落在关键帧时，重编码到下一个关键帧为止的一小段"""
        k_after = self._keyframe_after(s)
        start_on_key = self._on_keyframe(s)
        if start_on_key or not self.keyframes or (k_after and k_after >= e - 0.03):
            # 关键帧对齐 / 无索引 / 本段短于剩余 GOP：整段重编码更稳妥
            if start_on_key or not self.keyframes:
                self._cur_out = core_path
                return self._step_copy(s, e, dur, core_path, lo, hi)
            self._cur_out = core_path
            cmd = build_encode_cmd(self.src, s, dur, core_path, self.info, self.use_mp4)
            ok, err = self._run(cmd, dur, lambda f: self._emit(lo, hi, f))
            return ok == 0, err

        if k_after is None or not self.keyframes:
            self._cur_out = core_path
            cmd = build_encode_cmd(self.src, s, dur, core_path, self.info, self.use_mp4)
            ok, err = self._run(cmd, dur, lambda f: self._emit(lo, hi, f))
            return ok == 0, err

        # 切点到下一个关键帧的一段重编码，其余原样复制，最后拼接
        head_dur = max(0.01, k_after - s)
        body_dur = max(0.01, e - k_after)
        head_path = os.path.join(self._work, f"head_{self._seg_idx}{self.inter_ext}")
        body_path = os.path.join(self._work, f"body_{self._seg_idx}{self.inter_ext}")

        w_head = head_dur / (head_dur + body_dur)
        self.phase.emit(f"第 {self._seg_idx + 1} 段：切点重编码 {head_dur:.2f}s + 原样复制 {body_dur:.2f}s")
        self._cur_out = head_path
        cmd = build_encode_cmd(self.src, s, head_dur, head_path, self.info, self.use_mp4)
        ok, err = self._run(cmd, head_dur, lambda f: self._emit(lo, lo + (hi - lo) * 0.55 * w_head, f))
        if ok != 0:
            return False, err

        self._cur_out = body_path
        ok, err = self._step_copy(k_after, e, body_dur, body_path,
                                  lo + (hi - lo) * 0.55 * w_head, lo + (hi - lo) * 0.94)
        if not ok:
            return False, err

        list_file = os.path.join(self._work, f"core_list_{self._seg_idx}.txt")
        write_concat_list([head_path, body_path], list_file)
        self._cur_out = core_path
        cmd = build_concat_cmd(list_file, core_path, self.use_mp4)
        ok, err = self._run(cmd, dur, lambda f: self._emit(lo + (hi - lo) * 0.94, hi, f))
        return ok == 0, err

    def _build_with_title(self, s, e, dur, core_path, lo, hi, secs=None):
        """片段开头烧录赛事信息：字幕条显示 secs 秒，重编码到下一个关键帧，其后原样复制"""
        secs = float(self.title_seconds if secs is None else secs)
        head_end = e
        k = self._keyframe_after(s + secs - 0.001)
        if k is not None and k < e - 0.03:
            head_end = k
        head_dur = max(0.01, head_end - s)
        if head_dur >= dur - 0.03:
            self._cur_out = core_path
            self.phase.emit(f"第 {self._seg_idx + 1} 段：整段加入赛事信息字幕")
            cmd = build_overlay_head_cmd(self.src, s, dur, core_path, self.info,
                                         self.banner, secs, self.use_mp4)
            ok, err = self._run(cmd, dur, lambda f: self._emit(lo, hi, f))
            return ok == 0, err

        body_dur = max(0.01, e - head_end)
        head_path = os.path.join(self._work, f"title_{self._seg_idx}{self.inter_ext}")
        body_path = os.path.join(self._work, f"titlebody_{self._seg_idx}{self.inter_ext}")
        w_head = head_dur / (head_dur + body_dur)
        self.phase.emit(
            f"第 {self._seg_idx + 1} 段：片头字幕 {head_dur:.2f}s 重编码 + 其余原样复制 {body_dur:.2f}s")
        self._cur_out = head_path
        # 字幕条显示时长延长到这一个关键帧为止，避免重编码段与复制段在边界处衔接出重复画面
        head_show = max(secs, head_dur)
        cmd = build_overlay_head_cmd(self.src, s, head_dur, head_path, self.info,
                                     self.banner, head_show, self.use_mp4)
        ok, err = self._run(cmd, head_dur,
                            lambda f: self._emit(lo, lo + (hi - lo) * 0.55 * w_head, f))
        if ok != 0:
            return False, err

        self._cur_out = body_path
        ok, err = self._step_copy(head_end, e, body_dur, body_path,
                                  lo + (hi - lo) * 0.55 * w_head, lo + (hi - lo) * 0.94)
        if not ok:
            return False, err

        list_file = os.path.join(self._work, f"core_list_{self._seg_idx}.txt")
        write_concat_list([head_path, body_path], list_file)
        self._cur_out = core_path
        cmd = build_concat_cmd(list_file, core_path, self.use_mp4)
        ok, err = self._run(cmd, dur, lambda f: self._emit(lo + (hi - lo) * 0.94, hi, f))
        return ok == 0, err

    def _step_copy(self, s, e, dur, out, lo, hi):
        """流复制。ffmpeg 的 -ss 在部分容器（如 OBS 录制的 mkv）会退回上一个关键帧，
        多带出前面约一个 GOP 的画面（表现为片段开头重复）。这里复制后核对实际时长，
        按多出的部分前移起点重试，直到与请求时长一致。"""
        start = float(s)
        for attempt in range(4):
            emit = (lambda f: self._emit(lo, hi, f)) if attempt == 0 else (lambda f: None)
            cmd = build_copy_cmd(self.src, start, e, out, self.use_mp4)
            ok, err = self._run(cmd, dur, emit)
            if ok != 0:
                return False, err
            try:
                actual = float(probe_video(out).get("duration") or 0.0)
            except Exception:
                actual = 0.0
            if actual <= 0.01:
                return True, ""
            excess = actual - dur          # >0：起点被退回，多带了前摇
            gap = dur - actual             # >0：起点被推后，少了内容
            if excess <= 0.30 and gap <= 0.30:
                return True, ""
            if excess > 0.30:
                start = start + (excess - 0.02)
                if start >= e - 0.5:
                    return True, ""
                continue
            start = max(0.0, start - (gap - 0.02))
        return True, ""


# ---------------------------------------------------------------- 工程文件

class MergeWorker(ExportWorker):
    """把选中的多个片段拼成一个视频：片头 + 各片段（可只在第一段带字幕条） + 片尾"""

    def __init__(self, src, segments, out_dir, stem, ext, info, keyframes,
                 precise, intro, outro, banner, title_seconds, out_path,
                 first_only=True, cover=True, parent=None):
        super().__init__(src, segments, out_dir, stem, ext, info, keyframes,
                         precise, intro, outro, banner, title_seconds, cover, parent)
        self.out_path = out_path
        self.first_only = bool(first_only)

    def _build_segment(self, seg, idx, norm_intro, norm_outro, final_path):
        # 只在合并后的第一段保留字幕条，其余段按原样复制/精准切割
        if idx > 0 and self.first_only:
            banner = self.banner
            pending = self._title_pending
            self.banner = ""
            try:
                return super()._build_segment(seg, idx, norm_intro, norm_outro, final_path)
            finally:
                self.banner = banner
                self._title_pending = pending
        return super()._build_segment(seg, idx, norm_intro, norm_outro, final_path)

    def run(self):
        self._work = tempfile.mkdtemp(prefix="vsplit_merge_")
        try:
            norm_intro = norm_outro = None
            prep_w = 0.0
            if self.intro and os.path.isfile(self.intro):
                self.phase.emit("正在归一化片头…")
                norm_intro, intro_dur = self._normalize_aux(self.intro, "intro")
                prep_w += 0.5
                if norm_intro and self.banner:
                    norm_intro, used, intro_dur = self._burn_intro_title(norm_intro, intro_dur)
                    self._title_pending = max(0.0, self.title_seconds - used)
            if self.outro and os.path.isfile(self.outro):
                self.phase.emit("正在归一化片尾…")
                norm_outro, _d = self._normalize_aux(self.outro, "outro")
                prep_w += 0.5
            if self.intro and not os.path.isfile(self.intro):
                self.phase.emit(f"片头文件不存在，已忽略：{self.intro}")
            if self.outro and not os.path.isfile(self.outro):
                self.phase.emit(f"片尾文件不存在，已忽略：{self.outro}")

            seg_durs = [max(0.01, s["end"] - s["start"]) for s in self.segments]
            total_w = sum(seg_durs) + prep_w + 0.5
            done_w = 0.0
            parts = []
            if norm_intro:
                parts.append(norm_intro)

            for i, seg in enumerate(self.segments):
                if self._cancel:
                    self._cleanup_partial()
                    self.failed.emit("已取消合并导出", [])
                    return
                self._seg_idx = i
                self.phase.emit(f"正在处理第 {i + 1}/{len(self.segments)} 段（共 {fmt_time(seg_durs[i], False)}）")
                piece = os.path.join(self._work, f"merge_{i}{self.inter_ext}")
                self._cur_out = piece
                ok, err = self._build_segment(seg, i, None, None, piece)
                if self._cancel:
                    self._cleanup_partial()
                    self.failed.emit("已取消合并导出", [])
                    return
                if not ok:
                    self._cleanup_partial()
                    self.failed.emit(f"第 {i + 1} 段处理失败：\n{err or '未知错误'}", [])
                    return
                parts.append(piece)
                done_w += seg_durs[i]
                self.overall.emit(min(1.0, (done_w + prep_w) / total_w))

            if norm_outro:
                parts.append(norm_outro)

            self.phase.emit("正在拼接为单个视频…")
            list_file = os.path.join(self._work, "merge_list.txt")
            write_concat_list(parts, list_file)
            self._cur_out = self.out_path
            cmd = build_concat_cmd(list_file, self.out_path, self.use_mp4)
            ok, err = self._run(cmd, max(0.02, sum(seg_durs)),
                                lambda f: self.overall.emit(0.95 + 0.05 * f))
            if ok != 0:
                self._cleanup_partial()
                self.failed.emit(f"合并失败：\n{err or '未知错误'}", [])
                return
            if not os.path.exists(self.out_path) or os.path.getsize(self.out_path) == 0:
                self._cleanup_partial()
                self.failed.emit("合并失败：输出文件为空", [])
                return
            self._write_cover(self.out_path, self.segments[0]["start"] if self.segments else 0.0)
            self.done.emit([self.out_path])
        finally:
            shutil.rmtree(self._work, ignore_errors=True)


def _migrate_legacy_projects(base, new_dir):
    """把旧版放在「文档」下的工程文件搬进新目录（exe 同级「工程」），仅执行一次。"""
    store = QSettings("Doubao", "VideoSplitter")
    try:
        if bool(store.value("legacy_migrated_v2", False, type=bool)):
            return
    except Exception:
        return
    if not base:
        return
    for legacy in LEGACY_APP_NAMES:
        old = os.path.join(base, legacy, "工程")
        if not os.path.isdir(old):
            continue
        try:
            names = os.listdir(old)
        except Exception:
            continue
        for name in names:
            if not name.lower().endswith(PROJECT_SUFFIX):
                continue
            src = os.path.join(old, name)
            dst = os.path.join(new_dir, name)
            try:
                if not os.path.exists(dst):
                    shutil.move(src, dst)
            except Exception:
                pass
    try:
        last = store.value("last_project", "")
        if last and last.lower().endswith(PROJECT_SUFFIX) and not os.path.exists(last):
            alt = os.path.join(new_dir, os.path.basename(last))
            if os.path.exists(alt):
                store.setValue("last_project", alt)
        store.setValue("legacy_migrated_v2", True)
        store.sync()
    except Exception:
        pass


class ProjectStore:
    def __init__(self, path=""):
        self.path = path
        self.created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.video_path = ""
        self.markers = []
        self.deleted_idx = []
        self.checked_idx = []
        self.precise = False

    @staticmethod
    def default_dir():
        """工程默认存放目录：exe 同级的「工程」文件夹（不可写时退回到文档）。"""
        d = os.path.join(app_dir(), "工程")
        try:
            os.makedirs(d, exist_ok=True)
            ok = os.access(d, os.W_OK)
        except OSError:
            ok = False
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if not ok:
            d = os.path.join(docs if docs else app_dir(), APP_NAME, "工程")
            os.makedirs(d, exist_ok=True)
        _migrate_legacy_projects(docs, d)
        return d

    @classmethod
    def new(cls):
        p = cls()
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        p.path = os.path.join(cls.default_dir(), ts + PROJECT_SUFFIX)
        return p

    def save(self):
        data = {
            "version": 2,
            "app": APP_NAME,
            "created_at": self.created_at,
            "video_path": self.video_path,
            "markers": [round(float(m), 3) for m in self.markers],
            "deleted_segments": sorted(int(i) for i in self.deleted_idx),
            "checked_segments": sorted(int(i) for i in self.checked_idx),
            "precise_cut": bool(self.precise),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        p = cls(path)
        p.created_at = data.get("created_at") or datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        p.video_path = data.get("video_path", "")
        p.markers = [float(x) for x in data.get("markers", [])]
        p.deleted_idx = [int(x) for x in data.get("deleted_segments", [])]
        p.checked_idx = [int(x) for x in data.get("checked_segments", [])]
        p.precise = bool(data.get("precise_cut", False))
        return p


# ---------------------------------------------------------------- 设置对话框

class SettingsDialog(QDialog):
    def __init__(self, parent, shortcuts, intro, outro, obs=None, schedules=None, general=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(880, 800)
        self.setStyleSheet(parent.styleSheet() if parent else "")
        self.shortcuts = dict(shortcuts)
        self.key_edits = {}
        self._obs_worker = None
        obs = dict(obs or {})
        self._schedules = list(schedules or [])
        general = dict(general or {})
        vinfo = getattr(parent, "video_info", None) or {}
        self._frame_w = int(vinfo.get("width") or 1920)
        self._frame_h = int(vinfo.get("height") or 1080)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # ---- 快捷键
        page1 = QWidget()
        lay1 = QVBoxLayout(page1)
        lay1.setContentsMargins(10, 10, 10, 10)
        gb1 = QGroupBox("快捷键（点击后按下新的组合键即可修改，按 Backspace 可清除）")
        box1 = QVBoxLayout(gb1)
        self.table = QTableWidget(len(SHORTCUT_DEFS), 2)
        self.table.setHorizontalHeaderLabels(["功能", "快捷键"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 260)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for row, (action, title, default) in enumerate(SHORTCUT_DEFS):
            item = QTableWidgetItem(title)
            self.table.setItem(row, 0, item)
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setKeySequence(QKeySequence(self.shortcuts.get(action, default)))
            self.table.setCellWidget(row, 1, edit)
            self.key_edits[action] = edit
        self.table.verticalHeader().setDefaultSectionSize(27)
        self.table.setStyleSheet("QTableWidget::item { padding: 0px 6px; }")
        box1.addWidget(self.table, 1)

        btn_reset = QPushButton("恢复默认快捷键")
        btn_reset.setFixedWidth(160)
        btn_reset.clicked.connect(self.reset_shortcuts)
        box1.addWidget(btn_reset, alignment=Qt.AlignmentFlag.AlignRight)
        lay1.addWidget(gb1, 1)
        tabs.addTab(page1, "快捷键")

        # ---- 片头 / 片尾
        page2 = QWidget()
        lay2 = QVBoxLayout(page2)
        lay2.setContentsMargins(10, 10, 10, 10)
        gb2 = QGroupBox("片头 / 片尾（会追加到每个导出的片段，留空则不添加）")
        form = QGridLayout(gb2)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        self.edit_intro = QLineEdit(intro)
        self.edit_intro.setReadOnly(True)
        self.edit_outro = QLineEdit(outro)
        self.edit_outro.setReadOnly(True)
        b1 = QPushButton("选择…")
        b1.clicked.connect(lambda: self.pick_file(self.edit_intro, "选择片头视频"))
        c1 = QPushButton("清除")
        c1.clicked.connect(lambda: self.edit_intro.setText(""))
        b2 = QPushButton("选择…")
        b2.clicked.connect(lambda: self.pick_file(self.edit_outro, "选择片尾视频"))
        c2 = QPushButton("清除")
        c2.clicked.connect(lambda: self.edit_outro.setText(""))
        form.addWidget(QLabel("片头视频"), 0, 0)
        form.addWidget(self.edit_intro, 0, 1)
        form.addWidget(b1, 0, 2)
        form.addWidget(c1, 0, 3)
        form.addWidget(QLabel("片尾视频"), 1, 0)
        form.addWidget(self.edit_outro, 1, 1)
        form.addWidget(b2, 1, 2)
        form.addWidget(c2, 1, 3)
        tip = QLabel("片头/片尾会自动缩放到原视频的分辨率与帧率后无损拼接，不影响原片段编码。")
        tip.setStyleSheet("color:#8b919c;")
        form.addWidget(tip, 2, 0, 1, 4)
        lay2.addWidget(gb2)
        lay2.addStretch(1)
        tabs.addTab(page2, "片头 / 片尾")

        # ---- OBS：连接 + 定时录制
        page3 = QWidget()
        lay3 = QVBoxLayout(page3)
        lay3.setContentsMargins(10, 10, 10, 10)

        gb3 = QGroupBox("OBS WebSocket 连接（OBS 侧需启用 WebSocket 服务器）")
        grid = QGridLayout(gb3)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.edit_obs_host = QLineEdit(obs.get("host") or "127.0.0.1")
        self.edit_obs_host.setPlaceholderText("例如 127.0.0.1 或局域网 IP")
        self.spin_obs_port = QSpinBox()
        self.spin_obs_port.setRange(1, 65535)
        self.spin_obs_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin_obs_port.setFixedWidth(90)
        try:
            self.spin_obs_port.setValue(int(obs.get("port") or 4455))
        except Exception:
            self.spin_obs_port.setValue(4455)
        self.edit_obs_pwd = QLineEdit(obs.get("password") or "")
        self.edit_obs_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_obs_pwd.setPlaceholderText("OBS → 工具 → WebSocket 服务器设置 中的密码")
        self.btn_obs_test = QPushButton("测试连接")
        self.btn_obs_test.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_obs_test.clicked.connect(self.test_obs)
        self.lbl_obs_test = QLabel("未测试")
        self.lbl_obs_test.setStyleSheet("color:#8b919c;")
        self.lbl_obs_test.setWordWrap(True)
        grid.addWidget(QLabel("IP 地址"), 0, 0)
        grid.addWidget(self.edit_obs_host, 0, 1)
        grid.addWidget(QLabel("端口"), 0, 2)
        grid.addWidget(self.spin_obs_port, 0, 3)
        grid.addWidget(QLabel("连接密码"), 1, 0)
        grid.addWidget(self.edit_obs_pwd, 1, 1, 1, 2)
        grid.addWidget(self.btn_obs_test, 1, 3)
        grid.addWidget(self.lbl_obs_test, 2, 0, 1, 4)
        lay3.addWidget(gb3)

        gb4 = QGroupBox("定时启动 OBS 录制（最多两个，每天到点自动开始录制）")
        sg = QGridLayout(gb4)
        sg.setHorizontalSpacing(10)
        sg.setVerticalSpacing(8)
        self.sched_rows = []
        defaults = ["20:00", "21:00"]
        for i in range(2):
            data = self._schedules[i] if i < len(self._schedules) else {"enabled": False, "time": defaults[i]}
            chk = QCheckBox(f"第 {i + 1} 个任务")
            chk.setChecked(bool(data.get("enabled")))
            tedit = QTimeEdit()
            tedit.setDisplayFormat("HH:mm")
            try:
                hh, mm = [int(x) for x in str(data.get("time") or defaults[i]).split(":")[:2]]
            except Exception:
                hh, mm = 20 + i, 0
            tedit.setTime(QTime(max(0, min(23, hh)), max(0, min(59, mm))))
            sg.addWidget(chk, i, 0)
            sg.addWidget(QLabel("开始时间"), i, 1)
            sg.addWidget(tedit, i, 2)
            sg.addWidget(QLabel("每天"), i, 3)
            self.sched_rows.append((chk, tedit))
        tip2 = QLabel("到点后会用上面的连接参数让 OBS 开始录制；OBS 需已启动、已配置好录制输出。"
                      "程序运行期间生效，错过时间点不会补录。")
        tip2.setStyleSheet("color:#8b919c;")
        tip2.setWordWrap(True)
        sg.addWidget(tip2, 2, 0, 1, 4)
        lay3.addWidget(gb4)
        lay3.addStretch(1)
        tabs.addTab(page3, "OBS 与定时录制")

        # ---- 赛事信息
        page5 = QWidget()
        lay5 = QVBoxLayout(page5)
        lay5.setContentsMargins(10, 10, 10, 10)
        gb6 = QGroupBox("赛事信息（叠加在片段开头的画面上）")
        fg = QGridLayout(gb6)
        fg.setHorizontalSpacing(10)
        fg.setVerticalSpacing(8)
        ev = dict(general.get("event") or {})
        self.chk_overlay = QCheckBox("导出时叠加赛事信息字幕条（总开关，关闭则单段与合并都不叠加）")
        self.chk_overlay.setChecked(bool(ev.get("enabled", True)))
        fg.addWidget(self.chk_overlay, 0, 0, 1, 3)

        self.chk_merge_all = QCheckBox("合并导出时每个片段都叠加（不勾选＝只在合并后的第一段开头叠加一次）")
        self.chk_merge_all.setChecked(bool(ev.get("merge_all", False)))
        self.chk_merge_all.setEnabled(self.chk_overlay.isChecked())
        fg.addWidget(self.chk_merge_all, 1, 0, 1, 3)
        self.chk_overlay.toggled.connect(self.chk_merge_all.setEnabled)

        self.chk_cover = QCheckBox("同时为每个导出视频生成封面图（<文件名>_封面.jpg，画面带赛事信息）")
        self.chk_cover.setChecked(bool(ev.get("cover", True)))
        fg.addWidget(self.chk_cover, 2, 0, 1, 3)

        self.edit_event_name = QLineEdit(str(ev.get("name") or ""))
        self.edit_event_name.setPlaceholderText("例如：2026 成都羽毛球公开赛（第一行，最醒目）")
        self.edit_event_venue = QLineEdit(str(ev.get("venue") or ""))
        self.edit_event_venue.setPlaceholderText("例如：成都高新区体育馆（第二行）")
        self.edit_event_credit = QLineEdit(str(ev.get("copyright") or "鱼跃羽毛球"))
        self.edit_event_credit.setPlaceholderText("例如：鱼跃羽毛球（第三行，小字）")
        fg.addWidget(QLabel("赛事名称"), 3, 0)
        fg.addWidget(self.edit_event_name, 3, 1, 1, 2)
        fg.addWidget(QLabel("赛事地点"), 4, 0)
        fg.addWidget(self.edit_event_venue, 4, 1, 1, 2)
        fg.addWidget(QLabel("版权"), 5, 0)
        fg.addWidget(self.edit_event_credit, 5, 1, 1, 2)

        self.spin_overlay_sec = QSpinBox()
        self.spin_overlay_sec.setRange(1, 15)
        self.spin_overlay_sec.setValue(max(1, min(15, int(round(float(ev.get("seconds", 3) or 3))))))
        self.spin_overlay_sec.setSuffix(" 秒")
        self.spin_overlay_sec.setFixedWidth(110)
        fg.addWidget(QLabel("显示时长"), 6, 0)
        fg.addWidget(self.spin_overlay_sec, 6, 1)
        tip5 = QLabel("字幕条只在片段开头这几秒出现，之后自动消失；画面其余部分原样复制，不重新编码。")
        tip5.setStyleSheet("color:#8b919c;")
        tip5.setWordWrap(True)
        fg.addWidget(tip5, 7, 0, 1, 3)

        pos_row = QHBoxLayout()
        pos_row.setSpacing(8)
        self.chk_pos_auto = QCheckBox("自动居中")
        self.chk_pos_auto.setChecked(bool(ev.get("pos_auto", True)))
        self.spin_pos_x = QSpinBox()
        self.spin_pos_x.setRange(0, 20000)
        self.spin_pos_x.setValue(int(ev.get("pos_x", 100) or 0))
        self.spin_pos_x.setFixedWidth(110)
        self.spin_pos_y = QSpinBox()
        self.spin_pos_y.setRange(0, 20000)
        self.spin_pos_y.setValue(int(ev.get("pos_y", 300) or 0))
        self.spin_pos_y.setFixedWidth(110)
        pos_row.addWidget(self.chk_pos_auto)
        pos_row.addSpacing(6)
        pos_row.addWidget(QLabel("X"))
        pos_row.addWidget(self.spin_pos_x)
        pos_row.addWidget(QLabel("Y"))
        pos_row.addWidget(self.spin_pos_y)
        pos_row.addWidget(QLabel("像素（文字左上角）"))
        pos_row.addStretch(1)
        fg.addWidget(QLabel("位置"), 8, 0)
        fg.addLayout(pos_row, 8, 1, 1, 2)
        tip6 = QLabel(f"按原视频像素计算，画面左上角为 (0,0)；当前预览按 {self._frame_w}×{self._frame_h} 计算。")
        tip6.setStyleSheet("color:#8b919c;")
        tip6.setWordWrap(True)
        fg.addWidget(tip6, 9, 0, 1, 3)

        self.lbl_banner_preview = QLabel()
        self.lbl_banner_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_banner_preview.setMinimumHeight(250)
        self.lbl_banner_preview.setStyleSheet(
            "QLabel { background:#14161b; border:1px solid #333844; border-radius:6px; }")
        fg.addWidget(self.lbl_banner_preview, 10, 0, 1, 3)

        btn_prev = QPushButton("刷新预览")
        btn_prev.setFixedWidth(120)
        btn_prev.clicked.connect(self.update_banner_preview)
        fg.addWidget(btn_prev, 11, 2, alignment=Qt.AlignmentFlag.AlignRight)
        lay5.addWidget(gb6)
        lay5.addStretch(1)
        tabs.addTab(page5, "赛事信息")
        for w_ in (self.edit_event_name, self.edit_event_venue, self.edit_event_credit):
            w_.textChanged.connect(self.update_banner_preview)
        self.chk_overlay.toggled.connect(lambda _c: self.update_banner_preview())
        self.spin_pos_x.valueChanged.connect(lambda _v: self.update_banner_preview())
        self.spin_pos_y.valueChanged.connect(lambda _v: self.update_banner_preview())
        self.chk_pos_auto.toggled.connect(self._on_pos_auto_toggled)
        self._on_pos_auto_toggled(self.chk_pos_auto.isChecked())
        self.update_banner_preview()

        # ---- 常规
        page4 = QWidget()
        lay4 = QVBoxLayout(page4)
        lay4.setContentsMargins(10, 10, 10, 10)
        gb5 = QGroupBox("关闭主窗口时的行为")
        fl = QFormLayout(gb5)
        fl.setHorizontalSpacing(10)
        fl.setVerticalSpacing(10)
        self.combo_close = QComboBox()
        self.combo_close.addItem("直接退出程序", "exit")
        self.combo_close.addItem("最小化到系统托盘（双击托盘图标恢复窗口）", "tray")
        idx = 0 if general.get("close_action") != "tray" else 1
        self.combo_close.setCurrentIndex(idx)
        self.combo_close.setMinimumWidth(360)
        self.combo_close.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        fl.addRow("点击窗口关闭按钮时", self.combo_close)
        tip3 = QLabel("选择「最小化到系统托盘」后，点关闭只会把窗口收进右下角托盘，"
                      "不会打断正在进行的导出；需要彻底退出时用托盘菜单的「退出程序」。")
        tip3.setStyleSheet("color:#8b919c;")
        tip3.setWordWrap(True)
        fl.addRow("", tip3)
        lay4.addWidget(gb5)
        lay4.addStretch(1)
        tabs.addTab(page4, "常规")

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    def obs_values(self):
        return {
            "host": self.edit_obs_host.text().strip() or "127.0.0.1",
            "port": int(self.spin_obs_port.value()),
            "password": self.edit_obs_pwd.text(),
        }

    def test_obs(self):
        if self._obs_worker is not None and self._obs_worker.isRunning():
            return
        cfg = self.obs_values()
        self.btn_obs_test.setEnabled(False)
        self.lbl_obs_test.setStyleSheet("color:#9aa1ac;")
        self.lbl_obs_test.setText("正在连接…")
        self._obs_worker = ObsTaskWorker(cfg["host"], cfg["port"], cfg["password"], "GetVersion", parent=self)
        self._obs_worker.done.connect(self._on_obs_tested)
        self._obs_worker.start()

    def _on_obs_tested(self, ok, msg):
        self.btn_obs_test.setEnabled(True)
        if ok:
            self.lbl_obs_test.setStyleSheet("color:#46b98a;")
        else:
            self.lbl_obs_test.setStyleSheet("color:#e07a72;")
        self.lbl_obs_test.setText(msg)

    def event_values(self):
        return {
            "enabled": self.chk_overlay.isChecked(),
            "merge_all": self.chk_merge_all.isChecked(),
            "cover": self.chk_cover.isChecked(),
            "name": self.edit_event_name.text().strip(),
            "venue": self.edit_event_venue.text().strip(),
            "copyright": self.edit_event_credit.text().strip(),
            "seconds": int(self.spin_overlay_sec.value()),
            "pos_auto": self.chk_pos_auto.isChecked(),
            "pos_x": int(self.spin_pos_x.value()),
            "pos_y": int(self.spin_pos_y.value()),
        }

    def _on_pos_auto_toggled(self, checked):
        self.spin_pos_x.setEnabled(not checked)
        self.spin_pos_y.setEnabled(not checked)
        self.update_banner_preview()

    def update_banner_preview(self, *_args):
        ev = self.event_values()
        if not (ev["name"] or ev["venue"]):
            self.lbl_banner_preview.setPixmap(QPixmap())
            self.lbl_banner_preview.setText("填入赛事名称 / 地点后即可预览叠加效果")
            return
        img = render_banner_preview(ev, self._frame_w, self._frame_h)
        if img is None:
            return
        target_w = max(420, min(720, self.lbl_banner_preview.width() - 24))
        pm = QPixmap.fromImage(img).scaledToWidth(
            target_w, Qt.TransformationMode.SmoothTransformation)
        self.lbl_banner_preview.setPixmap(pm)
        self.lbl_banner_preview.setText("")

    def pick_file(self, edit, title):
        path, _ = QFileDialog.getOpenFileName(self, title, "",
                                              "视频文件 (*.mp4 *.mov *.mkv *.m4v *.avi *.ts *.flv *.wmv *.webm);;所有文件 (*.*)")
        if path:
            edit.setText(path)

    def reset_shortcuts(self):
        for action, _title, default in SHORTCUT_DEFS:
            self.key_edits[action].setKeySequence(QKeySequence(default))

    def values(self):
        shortcuts = {}
        seen = {}
        for action, title, default in SHORTCUT_DEFS:
            seq = self.key_edits[action].keySequence().toString()
            shortcuts[action] = seq
            if seq:
                if seq in seen:
                    QMessageBox.warning(self, "快捷键冲突",
                                        f"「{title}」与「{seen[seq]}」使用了相同快捷键：{seq}\n请重新设置。")
                    return None
                seen[seq] = title
        schedules = [{"enabled": chk.isChecked(), "time": tedit.time().toString("HH:mm")}
                     for chk, tedit in self.sched_rows]
        return (shortcuts, self.edit_intro.text().strip(), self.edit_outro.text().strip(),
                self.obs_values(), schedules,
                {"close_action": self.combo_close.currentData(),
                 "event": self.event_values()})

    def accept(self):
        vals = self.values()
        if vals is None:
            return
        self.result_values = vals
        super().accept()


# ---------------------------------------------------------------- 主窗口

QSS = """
QMainWindow, QWidget { background: #1b1c21; color: #d7dae0; }
QLabel { color: #c8ccd4; }
QPushButton {
    background: #2b2e36; border: 1px solid #3a3f49; border-radius: 6px;
    padding: 6px 12px; color: #e3e6eb;
}
QPushButton:hover { background: #343842; border-color: #4a5160; }
QPushButton:pressed { background: #242730; }
QPushButton:disabled { color: #6b7079; background: #23252b; border-color: #2c2f36; }
QPushButton#primary {
    background: #2a8f68; border-color: #36a87c; font-weight: 600; padding: 7px 18px;
}
QPushButton#primary:hover { background: #31a578; }
QPushButton#primary:disabled { background: #24463a; border-color: #2c5344; color: #86a597; }
QPushButton#danger { background: #53302f; border-color: #6d3c3a; }
QPushButton#danger:hover { background: #633836; }
QTableWidget {
    background: #16171c; alternate-background-color: #191b20;
    gridline-color: #262a32; border: 1px solid #2b2f38; border-radius: 6px;
    selection-background-color: #2a3b4f; selection-color: #ffffff;
}
QHeaderView::section {
    background: #20232a; color: #9aa1ac; border: none; border-right: 1px solid #2b2f38;
    padding: 5px 6px;
}
QTableWidget QTableCornerButton::section { background: #20232a; border: none; }
QFrame#videoFrame { background: #0b0c0f; border: 1px solid #2b2f38; border-radius: 8px; }
QProgressDialog { background: #1f2128; }
QProgressBar { border: 1px solid #343841; border-radius: 5px; background: #15161b; text-align: center; color: #d7dae0; height: 16px; }
QProgressBar::chunk { background: #2a8f68; border-radius: 4px; }
QToolTip { background: #2b3038; color: #ffd77a; border: 1px solid #454b57; padding: 3px 6px; }
QCheckBox { color: #d7dae0; spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 3px; border: 1px solid #4a5160; background: #23252b; }
QCheckBox::indicator:checked { background: #2a8f68; border-color: #36a87c; }
QLineEdit { background: #23252b; border: 1px solid #3a3f49; border-radius: 5px; padding: 4px 8px; color: #d7dae0; }
QGroupBox { border: 1px solid #2f333c; border-radius: 6px; margin-top: 12px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #9aa1ac; }
QKeySequenceEdit { background: #23252b; border: 1px solid #3a3f49; border-radius: 5px; }
"""


class VideoView(QGraphicsView):
    """预览画布：按原视频宽高比居中缩放显示，方便抓图/截图。"""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.on_resize = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.on_resize:
            self.on_resize()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1220, 830)
        self.setStyleSheet(QSS)
        self._native_video_size = (0, 0)

        self.settings = QSettings("Doubao", "VideoSplitter")
        self.shortcuts = self._load_shortcuts()
        self.intro_path = self.settings.value("intro_path", "", type=str)
        self.outro_path = self.settings.value("outro_path", "", type=str)
        self.obs_cfg = {
            "host": self.settings.value("obs_host", "127.0.0.1", type=str) or "127.0.0.1",
            "port": int(self.settings.value("obs_port", 4455) or 4455),
            "password": self.settings.value("obs_password", "", type=str),
        }
        self.schedules = self._load_schedules()
        self.close_action = self.settings.value("close_action", "exit", type=str) or "exit"
        self.event_cfg = self._load_event_cfg()
        self._banner_dir = ""
        self._tray = None
        self._tray_tip_shown = False
        self._really_quit = False

        self.video_path = ""
        self.video_info = {}
        self.duration = 0.0
        self.keyframes = []
        self.markers = []              # 原始切点时间（未吸附）
        self.deleted_idx = set()       # 被删除的片段索引
        self.checked_idx = set()       # 勾选（用于合并导出）的片段索引
        self.segments = []             # 实际使用的时间区间
        self.selected_index = -1
        self.current_time = 0.0
        self.precise = self.settings.value("precise_cut", False, type=bool)
        self.use_fallback_preview = False

        self.project = None
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(700)
        self._autosave_timer.timeout.connect(self.save_project_silent)

        self._kf_worker = None
        self._export_worker = None
        self._progress_dlg = None
        self._play_until = None
        self._shortcut_objs = []
        self._export_ctx = (0, 1, 0.0)
        self._phase_text = ""

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(160)
        self._preview_timer.timeout.connect(self._grab_frame)

        self._pending_seek = None
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(40)
        self._seek_timer.timeout.connect(self._apply_seek)

        self._preview_proc = QProcess(self)
        self._preview_proc.finished.connect(self._on_frame_grab_finished)
        self._preview_file = os.path.join(tempfile.gettempdir(), "vsplit_preview_frame.png")

        self._build_ui()
        self._init_player()
        self.apply_shortcut_settings()
        self.apply_close_behavior()

        self._obs_worker = None
        self._sched_fired = {}
        self._sched_timer = QTimer(self)
        self._sched_timer.setInterval(20000)
        self._sched_timer.timeout.connect(self._check_schedules)
        self._sched_timer.start()

    # ---------------- 设置存取
    def _load_shortcuts(self):
        raw = self.settings.value("shortcuts_json", "", type=str)
        defaults = {a: d for a, _t, d in SHORTCUT_DEFS}
        if not raw:
            return defaults
        try:
            data = json.loads(raw)
            result = dict(defaults)
            result.update({k: v for k, v in data.items() if isinstance(v, str)})
            return result
        except Exception:
            return defaults

    def _load_event_cfg(self):
        ev = {
            "enabled": self.settings.value("event_overlay_on", True, type=bool),
            "merge_all": self.settings.value("event_merge_all", False, type=bool),
            "cover": self.settings.value("event_cover", True, type=bool),
            "name": self.settings.value("event_name", "", type=str),
            "venue": self.settings.value("event_venue", "", type=str),
            "copyright": self.settings.value("event_copyright", "鱼跃羽毛球", type=str),
            "seconds": float(self.settings.value("event_overlay_seconds", 3) or 3),
            "pos_auto": self.settings.value("event_pos_auto", True, type=bool),
            "pos_x": int(self.settings.value("event_pos_x", 100) or 0),
            "pos_y": int(self.settings.value("event_pos_y", 300) or 0),
        }
        return ev

    def _load_schedules(self):
        raw = self.settings.value("schedules_json", "", type=str)
        result = [{"enabled": False, "time": "20:00"}, {"enabled": False, "time": "21:00"}]
        if not raw:
            return result
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for i in range(min(2, len(data))):
                    item = data[i] or {}
                    result[i] = {"enabled": bool(item.get("enabled")),
                                 "time": str(item.get("time") or result[i]["time"])}
        except Exception:
            pass
        return result

    def save_settings(self):
        self.settings.setValue("shortcuts_json", json.dumps(self.shortcuts, ensure_ascii=False))
        self.settings.setValue("intro_path", self.intro_path)
        self.settings.setValue("outro_path", self.outro_path)
        self.settings.setValue("precise_cut", self.precise)
        self.settings.setValue("obs_host", self.obs_cfg.get("host", "127.0.0.1"))
        self.settings.setValue("obs_port", int(self.obs_cfg.get("port", 4455)))
        self.settings.setValue("obs_password", self.obs_cfg.get("password", ""))
        self.settings.setValue("schedules_json", json.dumps(self.schedules, ensure_ascii=False))
        self.settings.setValue("close_action", self.close_action)
        ev = self.event_cfg or {}
        self.settings.setValue("event_overlay_on", bool(ev.get("enabled", True)))
        self.settings.setValue("event_merge_all", bool(ev.get("merge_all", False)))
        self.settings.setValue("event_cover", bool(ev.get("cover", True)))
        self.settings.setValue("event_name", ev.get("name", ""))
        self.settings.setValue("event_venue", ev.get("venue", ""))
        self.settings.setValue("event_copyright", ev.get("copyright", ""))
        self.settings.setValue("event_overlay_seconds", float(ev.get("seconds", 3) or 3))
        self.settings.setValue("event_pos_auto", bool(ev.get("pos_auto", True)))
        self.settings.setValue("event_pos_x", int(ev.get("pos_x", 100) or 0))
        self.settings.setValue("event_pos_y", int(ev.get("pos_y", 300) or 0))

    def _check_schedules(self):
        """每 20 秒检查一次，命中时间点就通过 OBS 开始录制（同一天同一分钟只触发一次）。"""
        now = datetime.datetime.now()
        hm = now.strftime("%H:%M")
        for idx, sch in enumerate(self.schedules):
            if not (sch.get("enabled") and sch.get("time") == hm):
                continue
            key = f"{now.strftime('%Y-%m-%d')} {hm}"
            if self._sched_fired.get(idx) == key:
                continue
            self._sched_fired[idx] = key
            self._fire_schedule(idx, hm)

    def _fire_schedule(self, idx, hm):
        if self._obs_worker is not None and self._obs_worker.isRunning():
            self.lbl_status.setText(f"定时任务 {idx + 1}（{hm}）跳过：上一次 OBS 请求还没结束。")
            return
        self.lbl_status.setText(f"定时任务 {idx + 1}（{hm}）正在连接 OBS 启动录制…")
        cfg = self.obs_cfg or {}
        self._obs_worker = ObsTaskWorker(cfg.get("host", "127.0.0.1"), cfg.get("port", 4455),
                                         cfg.get("password", ""), "StartRecord", parent=self)
        self._obs_worker.done.connect(lambda ok, msg, i=idx, t=hm: self._on_schedule_done(i, t, ok, msg))
        self._obs_worker.start()

    def _on_schedule_done(self, idx, hm, ok, msg):
        if ok:
            self.lbl_status.setText(f"定时任务 {idx + 1}（{hm}）已开始 OBS 录制。")
        else:
            self.lbl_status.setText(f"定时任务 {idx + 1}（{hm}）启动失败：{msg.splitlines()[0]}")

    def apply_shortcut_settings(self):
        for sc in self._shortcut_objs:
            sc.setEnabled(False)
        self._shortcut_objs = []
        handlers = {
            "open_video": self.open_video,
            "open_project": self.open_project,
            "save_project": lambda: self.save_project_silent(force=True),
            "settings": self.open_settings,
            "add_split": self.add_split,
            "undo_split": self.undo_split,
            "clear_splits": self.clear_splits,
            "delete_segment": self.delete_selected,
            "mark_start_delete": self.mark_alternating_delete,
            "restore_segment": self.restore_selected,
            "export": self.export_segments,
            "play_pause": self.toggle_play,
            "back": lambda: self.nudge(-5),
            "forward": lambda: self.nudge(5),
        }
        for action, _title, default in SHORTCUT_DEFS:
            seq = self.shortcuts.get(action, default)
            if not seq:
                continue
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(handlers[action])
            self._shortcut_objs.append(sc)

    def open_settings(self):
        dlg = SettingsDialog(self, self.shortcuts, self.intro_path, self.outro_path,
                             self.obs_cfg, self.schedules,
                             {"close_action": self.close_action,
                              "event": dict(self.event_cfg or {})})
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_shortcuts, intro, outro, obs_cfg, schedules, general = dlg.result_values
            self.shortcuts = new_shortcuts
            self.intro_path = intro
            self.outro_path = outro
            self.obs_cfg = obs_cfg
            self.schedules = schedules
            self.close_action = (general or {}).get("close_action", "exit")
            ev = (general or {}).get("event")
            if isinstance(ev, dict):
                self.event_cfg = ev
            self.save_settings()
            self.apply_shortcut_settings()
            self.apply_close_behavior()
            self.update_intro_outro_hint()
            self.lbl_status.setText("设置已保存。")

    # ---------------- 关闭行为 / 系统托盘
    def apply_close_behavior(self):
        """按设置决定是否常驻托盘图标。"""
        want_tray = self.close_action == "tray"
        if not want_tray:
            if self._tray is not None:
                self._tray.hide()
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            icon = QIcon(os.path.join(app_dir(), "icon.ico"))
            if icon.isNull():
                icon = self.windowIcon()
            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip(APP_NAME)
            menu = QMenu()
            act_show = QAction("显示主界面", self)
            act_show.triggered.connect(self._restore_from_tray)
            act_quit = QAction("退出程序", self)
            act_quit.triggered.connect(self._quit_from_tray)
            menu.addAction(act_show)
            menu.addSeparator()
            menu.addAction(act_quit)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self):
        self._really_quit = True
        self.close()

    # ---------------- UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(9)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.btn_open = QPushButton("打开视频")
        self.btn_open.clicked.connect(self.open_video)
        self.btn_open_project = QPushButton("打开工程")
        self.btn_open_project.clicked.connect(self.open_project)
        self.btn_save_project = QPushButton("保存工程")
        self.btn_save_project.setToolTip("工程会在每次修改后自动保存，这里可手动立即保存")
        self.btn_save_project.clicked.connect(lambda: self.save_project_silent(force=True))
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setToolTip("快捷键 / 片头片尾")
        self.btn_settings.clicked.connect(self.open_settings)
        for b in (self.btn_open, self.btn_open_project, self.btn_save_project, self.btn_settings):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            top.addWidget(b)

        self.lbl_file = QLabel("未选择视频")
        self.lbl_file.setStyleSheet("color:#9aa1ac;")
        top.addWidget(self.lbl_file, 1)

        self.lbl_meta = QLabel("")
        self.lbl_meta.setStyleSheet("color:#8b919c;")
        top.addWidget(self.lbl_meta)
        root.addLayout(top)

        self.video_frame = QFrame()
        self.video_frame.setObjectName("videoFrame")
        fl = QVBoxLayout(self.video_frame)
        fl.setContentsMargins(4, 4, 4, 4)
        self.stack = QStackedWidget()
        self.preview_scene = QGraphicsScene()
        self.video_item = QGraphicsVideoItem()
        self.preview_scene.addItem(self.video_item)
        self.video_widget = VideoView(self.preview_scene)
        self.video_widget.on_resize = self._layout_video_item
        self.video_item.nativeSizeChanged.connect(self._on_native_size_changed)
        self.video_widget.setStyleSheet("background:#000; border:none;")
        self.video_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.video_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_widget.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.fallback_label = QLabel("打开视频后在这里预览画面")
        self.fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fallback_label.setStyleSheet("background:#000; color:#6b7079;")
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.fallback_label)
        fl.addWidget(self.stack)
        root.addWidget(self.video_frame, 10)

        self.timeline = TimelineWidget()
        self.timeline.seekRequested.connect(self.on_seek_requested)
        self.timeline.segmentSelected.connect(self.on_segment_selected)
        self.timeline.segmentToggled.connect(self.on_segment_toggled)
        root.addWidget(self.timeline)

        ctl = QHBoxLayout()
        ctl.setSpacing(8)
        self.btn_play = QPushButton("播放")
        self.btn_play.setFixedWidth(72)
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self.toggle_play)
        ctl.addWidget(self.btn_play)

        self.lbl_time = QLabel("00:00.000 / 00:00.000")
        self.lbl_time.setFont(QFont("Consolas", 10))
        self.lbl_time.setStyleSheet("color:#9fd3ff;")
        ctl.addWidget(self.lbl_time)
        ctl.addSpacing(4)

        self.btn_goback = QPushButton("−5s")
        self.btn_goback.setFixedWidth(52)
        self.btn_goback.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_goback.clicked.connect(lambda: self.nudge(-5))
        self.btn_gofwd = QPushButton("+5s")
        self.btn_gofwd.setFixedWidth(52)
        self.btn_gofwd.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_gofwd.clicked.connect(lambda: self.nudge(5))
        ctl.addWidget(self.btn_goback)
        ctl.addWidget(self.btn_gofwd)

        ctl.addSpacing(10)
        self.chk_precise = QCheckBox("精准切割（局部重编码）")
        self.chk_precise.setToolTip(
            "勾选后切点精确到毫秒：只把「切点→下一个关键帧」之间的一小段重新编码，其余码流原样复制。\n"
            "不勾选则切点自动对齐关键帧，全程不重编码。"
        )
        self.chk_precise.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chk_precise.blockSignals(True)
        self.chk_precise.setChecked(self.precise)
        self.chk_precise.blockSignals(False)
        self.chk_precise.stateChanged.connect(self.on_precise_changed)
        ctl.addWidget(self.chk_precise)

        ctl.addStretch(1)

        self.btn_split = QPushButton("添加分割点")
        self.btn_split.setObjectName("primary")
        self.btn_split.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_split.setToolTip("把当前预览位置加为分割点")
        self.btn_split.clicked.connect(self.add_split)
        ctl.addWidget(self.btn_split)

        self.btn_undo = QPushButton("撤销上个切点")
        self.btn_undo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_undo.clicked.connect(self.undo_split)
        ctl.addWidget(self.btn_undo)

        self.btn_clear = QPushButton("清空切点")
        self.btn_clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_clear.clicked.connect(self.clear_splits)
        ctl.addWidget(self.btn_clear)
        root.addLayout(ctl)

        seg_box = QFrame()
        seg_layout = QHBoxLayout(seg_box)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(10)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["选", "片段", "起点", "终点", "时长", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 40)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (2, 3, 4, 5):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table.cellClicked.connect(self.on_table_clicked)
        self.table.cellDoubleClicked.connect(lambda r, c: self.on_segment_toggled(r))
        self.table.itemChanged.connect(self.on_table_item_changed)
        seg_layout.addWidget(self.table, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.btn_del = QPushButton("删除此段")
        self.btn_del.setObjectName("danger")
        self.btn_del.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_restore = QPushButton("恢复此段")
        self.btn_restore.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_restore.clicked.connect(self.restore_selected)
        self.btn_restore_all = QPushButton("全部恢复")
        self.btn_restore_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_restore_all.clicked.connect(self.restore_all)
        self.btn_auto_del = QPushButton("自动删除")
        self.btn_auto_del.setObjectName("danger")
        self.btn_auto_del.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_auto_del.setToolTip("以选中片段为起点，此段及其后每隔一段自动标记为删除（快捷键 Ctrl+Shift+D）")
        self.btn_auto_del.clicked.connect(self.mark_alternating_delete)
        side.addWidget(self.btn_del)
        side.addWidget(self.btn_restore)
        side.addWidget(self.btn_restore_all)
        side.addSpacing(6)
        self.btn_check_all = QPushButton("全选")
        self.btn_check_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_check_all.setToolTip("勾选所有未标记删除的片段（用于合并导出）")
        self.btn_check_all.clicked.connect(lambda: self.set_all_checked(True))
        self.btn_uncheck_all = QPushButton("取消全选")
        self.btn_uncheck_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_uncheck_all.setToolTip("清空所有勾选")
        self.btn_uncheck_all.clicked.connect(lambda: self.set_all_checked(False))
        check_row = QHBoxLayout()
        check_row.setSpacing(6)
        check_row.addWidget(self.btn_check_all)
        check_row.addWidget(self.btn_uncheck_all)
        side.addLayout(check_row)
        side.addSpacing(6)
        side.addWidget(self.btn_auto_del)
        side.addStretch(1)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet("color:#8b919c;")
        self.lbl_summary.setWordWrap(True)
        side.addWidget(self.lbl_summary)
        seg_layout.addLayout(side)
        root.addWidget(seg_box, 4)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("提示：未勾选精准切割时，切点会自动对齐关键帧；导出始终原样复制码流。")
        self.lbl_status.setStyleSheet("color:#787f8a;")
        bottom.addWidget(self.lbl_status, 1)

        self.btn_folder = QPushButton("打开输出目录")
        self.btn_folder.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_folder.setEnabled(False)
        self.btn_folder.clicked.connect(self.open_out_dir)
        bottom.addWidget(self.btn_folder)

        self.btn_export = QPushButton("导出有效片段")
        self.btn_export.setObjectName("primary")
        self.btn_export.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_segments)
        bottom.addWidget(self.btn_export)

        self.btn_merge = QPushButton("合并导出")
        self.btn_merge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_merge.setEnabled(False)
        self.btn_merge.setToolTip("把勾选的（未删除）片段按顺序拼成一个视频导出")
        self.btn_merge.clicked.connect(self.merge_segments)
        bottom.addWidget(self.btn_merge)
        root.addLayout(bottom)

        self.lbl_summary.setVisible(True)

    def _init_player(self):
        self.audio_out = QAudioOutput(self)
        self.audio_out.setVolume(1.0)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video_item)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_player_duration)
        self.player.playbackStateChanged.connect(self.on_playstate_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.errorOccurred.connect(self.on_player_error)

    def update_intro_outro_hint(self):
        parts = []
        if self.intro_path:
            parts.append("片头：" + os.path.basename(self.intro_path))
        if self.outro_path:
            parts.append("片尾：" + os.path.basename(self.outro_path))
        ev = self.event_cfg or {}
        if ev.get("enabled") and (ev.get("name") or ev.get("venue")):
            title = ev.get("name") or ev.get("venue") or ev.get("copyright")
            parts.append(f"赛事信息（{title}）叠加开头 {int(ev.get('seconds') or 3)}s")
        if parts:
            self.lbl_status.setText("导出将追加 " + "；".join(parts) + "。")

    # ---------------- 工程
    def _schedule_autosave(self):
        if self.project:
            self._autosave_timer.start()

    def _sync_project_state(self):
        if not self.project:
            return
        self.project.video_path = self.video_path
        self.project.markers = list(self.markers)
        self.project.deleted_idx = sorted(self.deleted_idx)
        self.project.checked_idx = sorted(self.checked_idx)
        self.project.precise = self.precise

    def save_project_silent(self, force=False):
        if not self.project:
            return
        self._sync_project_state()
        try:
            self.project.save()
            self.settings.setValue("last_project", self.project.path)
            if force:
                self.lbl_status.setText(f"工程已保存：{os.path.basename(self.project.path)}")
            self._update_title()
        except Exception as exc:
            if force:
                QMessageBox.warning(self, "保存工程失败", str(exc))
            else:
                self.lbl_status.setText(f"工程自动保存失败：{exc}")

    def _update_title(self):
        base = APP_NAME
        if self.project:
            base += f" - 工程 {os.path.basename(self.project.path)}"
        if self.video_path:
            base += f" · {os.path.basename(self.video_path)}"
        self.setWindowTitle(base)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开工程", ProjectStore.default_dir(),
            f"分割工程 (*{PROJECT_SUFFIX});;所有文件 (*.*)")
        if not path:
            return
        self.load_project(path)

    def load_project(self, path):
        try:
            proj = ProjectStore.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "工程读取失败", f"{path}\n{exc}")
            return
        # 打开其他工程前，先把当前工程的改动落盘，避免丢失
        if (self.project and getattr(self.project, "path", "")
                and os.path.normcase(self.project.path) != os.path.normcase(path)):
            self.save_project_silent()
        video = proj.video_path
        if not video or not os.path.isfile(video):
            QMessageBox.warning(self, "原视频不存在",
                                f"工程记录的原视频不存在或已移动：\n{video or '（空）'}\n\n请选择新的视频文件。")
            video, _ = QFileDialog.getOpenFileName(
                self, "重新指定原视频", "",
                "视频文件 (*.mp4 *.mov *.mkv *.m4v *.avi *.ts *.flv *.wmv *.webm);;所有文件 (*.*)")
            if not video:
                self.lbl_status.setText("工程已加载，但原视频缺失，无法预览 / 导出。")
                self.project = proj
                self.markers = list(proj.markers)
                self.deleted_idx = set(proj.deleted_idx)
                self.checked_idx = set(proj.checked_idx)
                self.precise = proj.precise
                self.chk_precise.setChecked(self.precise)
                self._update_title()
                return
        if not self.load_video(video, reset_state=False):
            return
        self.project = proj
        self.project.video_path = video
        self.markers = list(proj.markers)
        self.deleted_idx = set(proj.deleted_idx)
        self.checked_idx = set(proj.checked_idx)
        self.precise = proj.precise
        self.chk_precise.setChecked(self.precise)
        self.rebuild_segments()
        self._schedule_autosave()
        self.settings.setValue("last_project", path)
        self._update_title()
        self.lbl_status.setText(f"已打开工程 {os.path.basename(path)}（{len(self.markers)} 个切点）")

    def restore_last_project(self):
        last = self.settings.value("last_project", "", type=str)
        if last and os.path.isfile(last):
            self.load_project(last)

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择原视频", "",
            "视频文件 (*.mp4 *.mov *.mkv *.m4v *.avi *.ts *.mts *.flv *.wmv *.webm *.mpg *.mpeg);;所有文件 (*.*)",
        )
        if not path:
            return
        if not self.load_video(path, reset_state=True):
            return
        self._create_new_project(path)
        self.lbl_status.setText(f"已新建工程 {os.path.basename(self.project.path)}（自动保存已开启）")

    def _create_new_project(self, video_path):
        self.project = ProjectStore.new()
        self.project.video_path = video_path
        self.project.markers = []
        self.project.deleted_idx = []
        self.project.precise = self.precise
        self.save_project_silent()

    # ---------------- 加载视频
    def _on_native_size_changed(self, size):
        if size and size.width() > 0 and size.height() > 0:
            self._native_video_size = (size.width(), size.height())
            self._layout_video_item()

    def _layout_video_item(self):
        """按原视频宽高比把画面居中铺进预览区，多余部分留黑边。"""
        vp = self.video_widget.viewport().size()
        vw, vh = max(1, vp.width()), max(1, vp.height())
        nw, nh = self._native_video_size
        if nw <= 0 or nh <= 0:
            nw, nh = vw, vh
        scale = min(vw / nw, vh / nh)
        w, h = nw * scale, nh * scale
        self.video_item.setSize(QSize(int(w), int(h)))
        self.video_item.setPos((vw - w) / 2.0, (vh - h) / 2.0)
        self.preview_scene.setSceneRect(QRectF(0, 0, vw, vh))

    def load_video(self, path, reset_state=True):
        try:
            info = probe_video(path)
        except Exception as exc:
            QMessageBox.critical(self, "无法读取视频", f"读取视频信息失败：\n{exc}")
            return False
        if info["duration"] <= 0:
            QMessageBox.critical(self, "无法读取视频", "无法识别视频时长，请确认文件是否完整。")
            return False

        self.video_path = path
        self.video_info = info
        self.duration = info["duration"]
        self.keyframes = []
        self.selected_index = -1
        self.current_time = 0.0
        self.use_fallback_preview = False
        self.last_out_dir = ""
        self.btn_folder.setEnabled(False)
        self._play_until = None
        if reset_state:
            self.markers = []
            self.deleted_idx = set()
            self.precise = self.settings.value("precise_cut", False, type=bool)
            self.chk_precise.setChecked(self.precise)

        self.lbl_file.setText(os.path.basename(path))
        self.lbl_file.setToolTip(path)
        meta = f"{info['width']}×{info['height']}"
        if info["fps"]:
            meta += f" · {info['fps']:.3f} fps"
        if info["vcodec"]:
            meta += f" · {info['vcodec']}"
        if info["bitrate"]:
            meta += f" · {info['bitrate'] // 1000} kbps"
        meta += f" · {human_size(info['size'])}"
        self.lbl_meta.setText(meta)

        self.stack.setCurrentWidget(self.video_widget)
        self.audio_out.setVolume(0.0)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPosition(0)
        self.player.play()
        QTimer.singleShot(380, self._pause_after_load)
        self._native_video_size = (info["width"], info["height"])
        QTimer.singleShot(0, self._layout_video_item)

        self.timeline.set_duration(self.duration)
        self.rebuild_segments()
        self.update_time_label(0.0)
        self.btn_export.setEnabled(True)
        self.btn_split.setEnabled(True)
        self._update_title()

        if self._kf_worker and self._kf_worker.isRunning():
            self._kf_worker.stop()
            self._kf_worker.wait(800)
        self._kf_worker = KeyframeWorker(path, self.duration, info.get("start_time", 0.0))
        self._kf_worker.progress.connect(self.on_keyframe_progress)
        self._kf_worker.finished_keys.connect(self.on_keyframes_ready)
        self._kf_worker.start()
        self.lbl_status.setText("正在建立关键帧索引（用于无损切点）…")
        return True

    # ---------------- 播放器
    def _pause_after_load(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            QTimer.singleShot(120, lambda: self.player.setPosition(0))
        self.audio_out.setVolume(1.0)

    def on_player_duration(self, d):
        if d > 0 and self.duration <= 0:
            self.duration = d / 1000.0
            self.timeline.set_duration(self.duration)
            self.rebuild_segments()

    def on_position_changed(self, ms):
        t = ms / 1000.0
        if self._play_until is not None and t >= self._play_until - 0.03:
            self._play_until = None
            self.player.pause()
        self.current_time = t
        self.timeline.set_position(t)
        self.update_time_label(t)

    def on_playstate_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("暂停" if playing else "播放")

    def on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.InvalidMedia and self.video_path:
            self.enable_fallback_preview("该视频无法用系统播放器解码，已切换为逐帧预览模式（点击时间轴即可查看画面）")

    def on_player_error(self, err, msg):
        if self.video_path:
            self.enable_fallback_preview(f"内置播放器无法解码该视频（{msg}），已切换为逐帧预览模式")

    def enable_fallback_preview(self, note):
        if self.use_fallback_preview:
            return
        self.use_fallback_preview = True
        self.stack.setCurrentWidget(self.fallback_label)
        self.btn_play.setEnabled(False)
        self.player.pause()
        self.lbl_status.setText(note)
        self._preview_timer.start()

    # ---------------- 时间轴交互
    def on_seek_requested(self, t):
        if not self.video_path:
            return
        self._play_until = None
        t = max(0.0, min(t, self.duration))
        self.current_time = t
        self.timeline.set_position(t)
        self.update_time_label(t)
        if self.use_fallback_preview:
            self._preview_timer.start()
            return
        self._pending_seek = t
        if not self._seek_timer.isActive():
            self._apply_seek()
            self._seek_timer.start()

    def _apply_seek(self):
        if self._pending_seek is None:
            return
        t, self._pending_seek = self._pending_seek, None
        self.player.pause()
        self.player.setPosition(int(t * 1000))

    def on_segment_selected(self, idx):
        self.selected_index = idx
        self.timeline.set_selected(idx)
        if 0 <= idx < len(self.segments) and self.table.currentRow() != idx:
            self.table.blockSignals(True)
            self.table.selectRow(idx)
            self.table.blockSignals(False)
        self.update_buttons()

    def on_table_clicked(self, row, col):
        if not (0 <= row < len(self.segments)):
            return
        if col == 0:
            # 点复选框列只选中行，不自动播放，免得误触打断勾选
            self.on_segment_selected(row)
            return
        self.on_segment_selected(row)
        self.play_segment(row)

    def set_all_checked(self, checked=True):
        """全选 / 取消全选：全选只勾选未标记删除的片段（删除段落不会参与合并导出）"""
        if not self.segments:
            return
        self.table.blockSignals(True)
        for i in range(len(self.segments)):
            item = self.table.item(i, 0)
            if item is None:
                continue
            on = bool(checked) and not self.segments[i].get("deleted")
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        if checked:
            self.checked_idx = {i for i in range(len(self.segments))
                                if not self.segments[i].get("deleted")}
            self.lbl_status.setText(f"已勾选 {len(self.checked_idx)} 个可导出片段（已删除的片段自动跳过）。")
        else:
            self.checked_idx = set()
            self.lbl_status.setText("已取消全部勾选。")
        self.update_buttons()
        self._schedule_autosave()

    def on_table_item_changed(self, item):
        if item is None or item.column() != 0:
            return
        row = item.row()
        if item.checkState() == Qt.CheckState.Checked:
            self.checked_idx.add(row)
        else:
            self.checked_idx.discard(row)
        self.update_buttons()

    def play_segment(self, idx):
        """点击片段行：跳到该片段起点并自动播放到片段结尾"""
        if not (0 <= idx < len(self.segments)):
            return
        seg = self.segments[idx]
        self.on_seek_requested(seg["start"])
        if self.use_fallback_preview or not self.video_path:
            return
        self._play_until = seg["end"]
        self.player.play()

    def on_segment_toggled(self, idx):
        if not (0 <= idx < len(self.segments)):
            return
        if idx in self.deleted_idx:
            self.deleted_idx.discard(idx)
        else:
            self.deleted_idx.add(idx)
        self.selected_index = idx
        self.rebuild_segments(keep_selection=True)
        self._schedule_autosave()
        self.update_buttons()

    def nudge(self, delta):
        if not self.video_path:
            return
        self.on_seek_requested(self.current_time + delta)

    def toggle_play(self):
        if not self.video_path or self.use_fallback_preview:
            return
        self._play_until = None
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.current_time >= self.duration - 0.05:
                self.on_seek_requested(0.0)
            self.player.play()

    # ---------------- 切点 / 片段
    def on_precise_changed(self, state):
        self.precise = bool(state)
        self.settings.setValue("precise_cut", self.precise)
        self.rebuild_segments(keep_selection=True)
        self._schedule_autosave()
        if self.precise:
            self.lbl_status.setText("已开启精准切割：切点按毫秒精确，切点所在的关键帧片段会重新编码。")
        else:
            self.lbl_status.setText("已关闭精准切割：切点对齐关键帧，全程不重编码。")

    def snap_time(self, t):
        if not self.keyframes:
            return t
        i = bisect.bisect_left(self.keyframes, t)
        cands = []
        if i < len(self.keyframes):
            cands.append(self.keyframes[i])
        if i - 1 >= 0:
            cands.append(self.keyframes[i - 1])
        if not cands:
            return t
        return min(cands, key=lambda k: abs(k - t))

    def effective_markers(self):
        if self.precise:
            return sorted(self.markers)
        out = []
        for m in sorted(self.markers):
            t = self.snap_time(m)
            if not out or abs(t - out[-1]) > 0.02:
                out.append(t)
        return out

    def add_split(self):
        if not self.video_path:
            return
        raw = self.current_time
        if raw <= 0.05 or raw >= self.duration - 0.05:
            QMessageBox.information(self, "无法添加", "分割点不能放在视频的最开头或最结尾。")
            return
        eff = raw if self.precise else self.snap_time(raw)
        for m in self.effective_markers():
            if abs(m - eff) < 0.12:
                self.lbl_status.setText("该位置已经有关键帧分割点，无需重复添加。")
                return

        i = bisect.bisect_left(self.markers, raw)
        p = i + 1  # 新切点在边界数组中的位置
        new_deleted = set()
        for k in self.deleted_idx:
            if k >= p:
                new_deleted.add(k + 1)
            elif k == p - 1:
                new_deleted.add(k)
                new_deleted.add(k + 1)
            else:
                new_deleted.add(k)
        self.deleted_idx = new_deleted
        self.markers.insert(i, raw)
        self.selected_index = p
        self.rebuild_segments(keep_selection=True)
        self._schedule_autosave()
        self.update_buttons()
        if self.precise:
            self.lbl_status.setText(f"已在 {fmt_time(raw)} 添加分割点（精准切割：位置不做吸附）。")
        elif self.keyframes:
            self.lbl_status.setText(f"已在 {fmt_time(eff)} 添加分割点（已对齐到最近关键帧）。")
        else:
            self.lbl_status.setText(f"已在 {fmt_time(raw)} 添加分割点（关键帧索引仍未就绪）。")

    def undo_split(self):
        if not self.markers:
            return
        t = self.current_time
        eff = self.effective_markers()
        target = min(eff, key=lambda m: abs(m - t)) if eff else t
        if abs(target - t) > 2.0:
            target = eff[-1] if eff else self.markers[-1]
        # 找到对应原始切点
        idx = -1
        for i, m in enumerate(self.markers):
            if abs((m if self.precise else self.snap_time(m)) - target) < 0.03:
                idx = i
                break
        if idx < 0:
            idx = len(self.markers) - 1
        boundary = idx + 1
        merged = (boundary - 1 in self.deleted_idx) or (boundary in self.deleted_idx)
        new_deleted = set()
        for k in self.deleted_idx:
            if k < boundary - 1:
                new_deleted.add(k)
            elif k > boundary:
                new_deleted.add(k - 1)
        if merged:
            new_deleted.add(boundary - 1)
        self.deleted_idx = new_deleted
        removed = self.markers.pop(idx)
        self.rebuild_segments()
        self._schedule_autosave()
        self.lbl_status.setText(f"已撤销 {fmt_time(removed)} 处的分割点。")

    def clear_splits(self):
        if not self.markers and not self.deleted_idx:
            return
        parts = []
        if self.markers:
            parts.append(f"{len(self.markers)} 个分割点")
        if self.deleted_idx:
            parts.append(f"{len(self.deleted_idx)} 段删除标记")
        box = QMessageBox(self)
        box.setWindowTitle("清空切点")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"确定要清空所有{'和'.join(parts)}吗？")
        box.setInformativeText("清空后视频会恢复成一个完整片段，需要重新添加分割点，此操作不能撤销。")
        yes = box.addButton("确定清空", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        if box.clickedButton() != yes:
            self.lbl_status.setText("已取消清空切点。")
            return
        self.markers = []
        self.deleted_idx = set()
        self.selected_index = -1
        self.rebuild_segments()
        self._schedule_autosave()
        self.lbl_status.setText("已清空所有分割点与删除标记。")

    def rebuild_segments(self, keep_selection=False):
        old_sel = self.selected_index
        eff = self.effective_markers()
        bounds = [0.0] + eff + [self.duration]
        segs = []
        for i in range(len(bounds) - 1):
            s, e = bounds[i], bounds[i + 1]
            if e - s < 1e-6:
                continue
            segs.append({"start": s, "end": e, "deleted": i in self.deleted_idx})
        self.segments = segs
        self.checked_idx = {i for i in self.checked_idx if i < len(segs)}
        if keep_selection and 0 <= old_sel < len(segs):
            self.selected_index = old_sel
        elif not (0 <= self.selected_index < len(segs)):
            self.selected_index = -1
        self.timeline.set_markers(eff)
        self.timeline.set_segments(segs)
        self.timeline.set_selected(self.selected_index)
        self.refresh_table()
        self.update_summary()
        self.update_buttons()

    def refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.segments))
        kept_total = 0.0
        for i, seg in enumerate(self.segments):
            dur = seg["end"] - seg["start"]
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if i in self.checked_idx
                              else Qt.CheckState.Unchecked)
            items = [
                chk,
                QTableWidgetItem(f"片段 {i + 1}"),
                QTableWidgetItem(fmt_time(seg["start"])),
                QTableWidgetItem(fmt_time(seg["end"])),
                QTableWidgetItem(fmt_time(dur, False) if dur >= 1 else f"{dur:.2f}s"),
                QTableWidgetItem("已删除" if seg.get("deleted") else "保留"),
            ]
            if seg.get("deleted"):
                for it in items[1:]:
                    it.setForeground(QColor("#c07f7f"))
            else:
                kept_total += dur
                items[5].setForeground(QColor("#6fd6aa"))
            for col, it in enumerate(items):
                if col == 0:
                    align = Qt.AlignmentFlag.AlignCenter
                elif col in (1, 4, 5):
                    align = Qt.AlignmentFlag.AlignCenter
                else:
                    align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                it.setTextAlignment(align)
                self.table.setItem(i, col, it)
        if 0 <= self.selected_index < len(self.segments):
            self.table.selectRow(self.selected_index)
        self.table.blockSignals(False)
        self.kept_total = kept_total

    def update_summary(self):
        kept = [s for s in self.segments if not s.get("deleted")]
        deleted = [s for s in self.segments if s.get("deleted")]
        total = getattr(self, "kept_total", sum(s["end"] - s["start"] for s in kept))
        self.lbl_summary.setText(
            f"共 {len(self.segments)} 段\n保留 {len(kept)} 段 · {fmt_time(total, False)}\n删除 {len(deleted)} 段"
        )

    def update_buttons(self):
        has = 0 <= self.selected_index < len(self.segments)
        self.btn_del.setEnabled(has and self.selected_index not in self.deleted_idx)
        self.btn_restore.setEnabled(has and self.selected_index in self.deleted_idx)
        self.btn_restore_all.setEnabled(bool(self.deleted_idx))
        self.btn_auto_del.setEnabled(has)
        self.btn_undo.setEnabled(bool(self.markers))
        self.btn_clear.setEnabled(bool(self.markers or self.deleted_idx))
        self.btn_export.setEnabled(bool(self.segments) and any(not s.get("deleted") for s in self.segments))
        self.btn_merge.setEnabled(self._merge_candidates() != [])

    def delete_selected(self):
        if 0 <= self.selected_index < len(self.segments) and self.selected_index not in self.deleted_idx:
            self.on_segment_toggled(self.selected_index)

    def restore_selected(self):
        if 0 <= self.selected_index < len(self.segments) and self.selected_index in self.deleted_idx:
            self.on_segment_toggled(self.selected_index)

    def mark_alternating_delete(self):
        """以选中片段为起点，此段及之后每隔一段自动标记为删除。"""
        if not (0 <= self.selected_index < len(self.segments)):
            self.lbl_status.setText("请先在片段列表里选中一段，再点「自动删除」。")
            return
        start = self.selected_index
        new_set = set(i for i in self.deleted_idx if i < start)
        marked = kept = 0
        for i in range(start, len(self.segments)):
            if (i - start) % 2 == 0:
                new_set.add(i)
                marked += 1
            else:
                kept += 1
        self.deleted_idx = new_set
        self.rebuild_segments(keep_selection=True)
        self._schedule_autosave()
        self.lbl_status.setText(
            f"已自动删除：从片段 {start + 1} 起每隔一段，删除 {marked} 段、保留 {kept} 段。")

    def restore_all(self):
        if not self.deleted_idx:
            return
        box = QMessageBox(self)
        box.setWindowTitle("全部恢复")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"确定要把已删除的 {len(self.deleted_idx)} 段全部恢复为保留吗？")
        box.setInformativeText("恢复后这些片段都会参与导出，此操作不能撤销（只能重新逐段删除）。")
        yes = box.addButton("确定恢复", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        if box.clickedButton() != yes:
            self.lbl_status.setText("已取消恢复。")
            return
        self.deleted_idx = set()
        self.rebuild_segments(keep_selection=True)
        self._schedule_autosave()
        self.lbl_status.setText("已恢复所有片段。")

    def update_time_label(self, t):
        self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(self.duration)}")

    def on_keyframe_progress(self, frac, count):
        self.lbl_status.setText(f"正在建立关键帧索引… {frac * 100:.0f}%（已找到 {count} 个关键帧）")

    def on_keyframes_ready(self, keys):
        self.keyframes = keys
        if not keys:
            self.lbl_status.setText("该视频未检测到关键帧索引，切点将直接使用点击位置。")
            return
        self.rebuild_segments(keep_selection=True)
        self.lbl_status.setText(
            f"关键帧索引完成：共 {len(keys)} 个关键帧，间隔约 {self._gop_hint(keys)}。")

    def _gop_hint(self, keys):
        if len(keys) < 2:
            return "—"
        gaps = sorted(g for g in (keys[i + 1] - keys[i] for i in range(min(len(keys) - 1, 200))) if g > 0.01)
        if not gaps:
            return "—"
        return f"{gaps[len(gaps) // 2]:.2f}s"

    # ---------------- 逐帧预览
    def _grab_frame(self):
        if not (self.video_path and self.use_fallback_preview):
            return
        if self._preview_proc.state() != QProcess.ProcessState.NotRunning:
            self._preview_proc.kill()
            self._preview_proc.waitForFinished(1000)
        args = [
            "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-ss", f"{self.current_time:.3f}", "-i", self.video_path,
            "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", self._preview_file,
        ]
        self._preview_proc.start(FFMPEG, args)

    def _on_frame_grab_finished(self, code, status):
        if code == 0 and os.path.exists(self._preview_file):
            pm = QPixmap(self._preview_file)
            if not pm.isNull():
                self.fallback_label.setPixmap(pm)
                self.stack.setCurrentWidget(self.fallback_label)
                try:
                    os.remove(self._preview_file)
                except OSError:
                    pass

    # ---------------- 导出
    def _prepare_banner(self):
        """按当前赛事信息生成透明底字幕条 PNG，返回 (路径, 显示秒数)。"""
        ev = self.event_cfg or {}
        if not (ev.get("enabled") and (ev.get("name") or ev.get("venue"))):
            return "", 0.0
        info_w = self.video_info.get("width") or 1920
        info_h = self.video_info.get("height") or 1080
        img = build_banner_image(ev, info_w, info_h)
        if img is None:
            return "", 0.0
        self._cleanup_banner()
        self._banner_dir = tempfile.mkdtemp(prefix="vsplit_title_")
        banner = os.path.join(self._banner_dir, "banner.png")
        if img.save(banner):
            return banner, float(ev.get("seconds") or 3)
        self._cleanup_banner()
        return "", 0.0

    def _merge_candidates(self):
        return [i for i in sorted(self.checked_idx)
                if 0 <= i < len(self.segments) and not self.segments[i].get("deleted")]

    def merge_segments(self):
        if not self.video_path:
            QMessageBox.information(self, "还没有视频", "请先打开原视频。")
            return
        idxs = self._merge_candidates()
        if not idxs:
            QMessageBox.information(self, "没有勾选片段",
                                    "请先在片段列表左侧勾选要合并的片段；\n"
                                    "已标记为删除的片段不会参与合并。")
            return
        segs = [self.segments[i] for i in idxs]

        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        out_dir = os.path.join(os.path.dirname(self.video_path), stem)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "无法创建目录", f"{out_dir}\n{exc}")
            return
        ext = os.path.splitext(self.video_path)[1].lower()
        if ext not in (".mp4", ".mov", ".mkv", ".m4v", ".ts", ".avi", ".flv", ".wmv",
                       ".webm", ".mts", ".mpg", ".mpeg"):
            ext = ".mp4"

        default_name = f"{stem}_合并"
        name, ok = QInputDialog.getText(self, "合并导出", "输出文件名（可改，不用写扩展名）：",
                                        text=default_name)
        if not ok:
            return
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name.strip())
        if not name:
            QMessageBox.warning(self, "文件名不能为空", "请重新点击「合并导出」并填写文件名。")
            return
        if not name.lower().endswith(ext):
            name += ext
        out_path = os.path.join(out_dir, name)
        if os.path.exists(out_path):
            self.lbl_status.setText(f"输出文件已存在，将被覆盖：{name}")

        banner, title_seconds = self._prepare_banner()
        merge_all = bool((self.event_cfg or {}).get("merge_all", False))
        want_cover = bool((self.event_cfg or {}).get("cover", True))
        total = sum(s["end"] - s["start"] for s in segs)
        self.last_out_dir = out_dir
        self.btn_folder.setEnabled(True)

        self._progress_dlg = QProgressDialog(self)
        self._progress_dlg.setWindowTitle("正在合并导出")
        self._progress_dlg.setLabelText(f"准备合并 {len(segs)} 个片段（共 {fmt_time(total, False)}）…")
        self._progress_dlg.setCancelButtonText("取消导出")
        self._progress_dlg.setRange(0, 1000)
        self._progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dlg.setMinimumDuration(0)
        self._progress_dlg.setAutoClose(False)
        self._progress_dlg.setAutoReset(False)
        self._progress_dlg.canceled.connect(self._cancel_export)
        self._progress_dlg.show()

        self.btn_export.setEnabled(False)
        self.btn_merge.setEnabled(False)
        self.btn_open.setEnabled(False)

        self._export_worker = MergeWorker(
            self.video_path, segs, out_dir, os.path.splitext(name)[0], ext, self.video_info,
            list(self.keyframes), self.precise, self.intro_path, self.outro_path,
            banner, title_seconds, out_path, first_only=not merge_all, cover=want_cover)
        self._export_worker.phase.connect(self._on_phase)
        self._export_worker.overall.connect(self._on_overall)
        self._export_worker.done.connect(self._on_merge_done)
        self._export_worker.failed.connect(self._on_merge_failed)
        self._export_worker.start()

    def _on_merge_done(self, outputs):
        if self._progress_dlg:
            self._progress_dlg.close()
        self._cleanup_banner()
        self.btn_export.setEnabled(True)
        self.btn_merge.setEnabled(self._merge_candidates() != [])
        self.btn_open.setEnabled(True)
        self.btn_folder.setEnabled(True)
        path = outputs[0] if outputs else ""
        self.lbl_status.setText(f"合并导出完成：{os.path.basename(path)} → {self.last_out_dir}")
        box = QMessageBox(self)
        box.setWindowTitle("合并导出完成")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("已合并导出 1 个视频")
        box.setInformativeText("文件：" + path)
        play_btn = box.addButton("打开文件", QMessageBox.ButtonRole.ActionRole)
        folder_btn = box.addButton("打开目录", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == play_btn and path and os.path.isfile(path):
            os.startfile(path)
        elif clicked == folder_btn:
            os.startfile(self.last_out_dir)
        self._schedule_autosave()

    def _on_merge_failed(self, message, outputs):
        if self._progress_dlg:
            self._progress_dlg.close()
        self._cleanup_banner()
        self.btn_export.setEnabled(True)
        self.btn_merge.setEnabled(self._merge_candidates() != [])
        self.btn_open.setEnabled(True)
        QMessageBox.warning(self, "合并导出未完成", message)

    def open_out_dir(self):
        d = getattr(self, "last_out_dir", "")
        if d and os.path.isdir(d):
            os.startfile(d)

    def export_segments(self):
        if not self.segments:
            return
        kept = [s for s in self.segments if not s.get("deleted")]
        if not kept:
            QMessageBox.information(self, "没有可导出的内容", "所有片段都被标记为删除，请至少保留一段。")
            return

        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        out_dir = os.path.join(os.path.dirname(self.video_path), stem)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "无法创建目录", f"{out_dir}\n{exc}")
            return

        ext = os.path.splitext(self.video_path)[1].lower()
        if ext not in (".mp4", ".mov", ".mkv", ".m4v", ".ts", ".avi", ".flv", ".wmv", ".webm", ".mts", ".mpg", ".mpeg"):
            ext = ".mp4"

        total = sum(s["end"] - s["start"] for s in kept)
        self.last_out_dir = out_dir
        self.btn_folder.setEnabled(True)
        self._phase_text = "准备导出…"

        banner, title_seconds = self._prepare_banner()
        want_cover = bool((self.event_cfg or {}).get("cover", True))
        if title_seconds:
            self._phase_text = f"将在每个片段开头叠加赛事信息 {title_seconds:.0f}s"

        self._progress_dlg = QProgressDialog(self)
        self._progress_dlg.setWindowTitle("正在导出片段")
        self._progress_dlg.setLabelText(f"准备导出 {len(kept)} 个片段（共 {fmt_time(total, False)}）…")
        self._progress_dlg.setCancelButtonText("取消导出")
        self._progress_dlg.setRange(0, 1000)
        self._progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dlg.setMinimumDuration(0)
        self._progress_dlg.setAutoClose(False)
        self._progress_dlg.setAutoReset(False)
        self._progress_dlg.canceled.connect(self._cancel_export)
        self._progress_dlg.show()

        self.btn_export.setEnabled(False)
        self.btn_open.setEnabled(False)

        self._export_worker = ExportWorker(
            self.video_path, kept, out_dir, stem, ext, self.video_info,
            list(self.keyframes), self.precise, self.intro_path, self.outro_path,
            banner, title_seconds, cover=want_cover)
        self._export_worker.phase.connect(self._on_phase)
        self._export_worker.seg_started.connect(self._on_seg_started)
        self._export_worker.seg_progress.connect(self._on_seg_progress)
        self._export_worker.overall.connect(self._on_overall)
        self._export_worker.done.connect(self._on_export_done)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def _cancel_export(self):
        if self._export_worker:
            self._export_worker.cancel()
            self._progress_dlg.setLabelText("正在取消…")

    def _on_phase(self, text):
        self._phase_text = text
        if self._progress_dlg:
            self._progress_dlg.setLabelText(text)

    def _on_seg_started(self, idx, total, dur):
        self._export_ctx = (idx, total, dur)

    def _on_seg_progress(self, idx, frac):
        ctx = getattr(self, "_export_ctx", (idx, 1, 0.0))
        if self._progress_dlg:
            self._progress_dlg.setLabelText(
                f"{self._phase_text}   （{frac * 100:.0f}%）")

    def _on_overall(self, value):
        if self._progress_dlg:
            self._progress_dlg.setValue(int(value * 1000))

    def _on_export_done(self, outputs):
        self._progress_dlg.close()
        self.btn_export.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_folder.setEnabled(True)
        self._cleanup_banner()
        self.lbl_status.setText(f"导出完成：{len(outputs)} 个文件 → {self.last_out_dir}")
        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"已导出 {len(outputs)} 个片段")
        info = "目录：" + self.last_out_dir + "\n\n命名：" + os.path.basename(outputs[0])
        if len(outputs) > 1:
            info += " … " + os.path.basename(outputs[-1])
        box.setInformativeText(info)
        open_btn = box.addButton("打开目录", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == open_btn:
            os.startfile(self.last_out_dir)
        self._schedule_autosave()

    def _on_export_failed(self, message, outputs):
        if self._progress_dlg:
            self._progress_dlg.close()
        self._cleanup_banner()
        self.btn_export.setEnabled(True)
        self.btn_open.setEnabled(True)
        QMessageBox.warning(self, "导出未完成", message)

    def _cleanup_banner(self):
        d = getattr(self, "_banner_dir", "")
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        self._banner_dir = ""

    def closeEvent(self, event):
        if (self.close_action == "tray" and not self._really_quit
                and self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable()):
            event.ignore()
            self.save_project_silent()
            self.save_settings()
            self.hide()
            if not self._tray_tip_shown:
                self._tray_tip_shown = True
                self._tray.showMessage(APP_NAME, "已最小化到系统托盘，双击图标可恢复窗口。",
                                       QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        self.save_project_silent()
        self.save_settings()
        if self._kf_worker and self._kf_worker.isRunning():
            self._kf_worker.stop()
            self._kf_worker.wait(1500)
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.cancel()
            self._export_worker.wait(3000)
        self.player.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------- 入口与自检

def run_selftest(src, out_dir, cuts, precise=False, intro="", outro=""):
    setup_bins()
    info = probe_video(src)
    print("视频信息:", info)
    keys = []
    iter_keyframes(src, keys.append, start_time=info.get("start_time", 0.0))
    print("关键帧数:", len(keys), "前几个:", keys[:6])
    bounds = [0.0] + cuts + [info["duration"]]
    segs = [{"start": bounds[i], "end": bounds[i + 1], "deleted": False} for i in range(len(bounds) - 1)]
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = os.path.splitext(src)[1]
    w = ExportWorker(src, segs, out_dir, stem, ext, info, keys, precise, intro, outro)
    w.run()
    print("输出:", w.outputs)


def main():
    setup_bins()

    if "--selftest" in sys.argv:
        args = sys.argv[sys.argv.index("--selftest") + 1:]
        precise = "--precise" in args
        args = [a for a in args if a != "--precise"]
        intro = outro = ""
        if "--intro" in args:
            i = args.index("--intro")
            intro = args[i + 1]
            del args[i:i + 2]
        if "--outro" in args:
            i = args.index("--outro")
            outro = args[i + 1]
            del args[i:i + 2]
        src, out_dir = args[0], args[1]
        cuts = [float(x) for x in args[2:]]
        run_selftest(src, out_dir, cuts, precise, intro, outro)
        return

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon_path = os.path.join(app_dir(), "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        path, out_json = sys.argv[i + 1], sys.argv[i + 2]
        win = MainWindow()
        win.show()
        win.load_video(path)

        def report():
            data = {
                "status": int(win.player.mediaStatus().value),
                "has_video": win.player.hasVideo(),
                "duration": win.duration,
                "keyframes": len(win.keyframes),
                "project": win.project.path if win.project else "",
                "native": list(win._native_video_size),
                "position": win.player.position(),
                "state": int(win.player.playbackState().value),
                "websocket": _websocket is not None,
            }
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            app.quit()

        QTimer.singleShot(6000, report)
        app.exec()
        return

    if not FFMPEG:
        QMessageBox.critical(
            None, "缺少 ffmpeg",
            "未找到 ffmpeg.exe。\n请把 ffmpeg.exe 放到程序所在目录的 bin 文件夹后重试。",
        )
        return

    win = MainWindow()
    win.show()
    QTimer.singleShot(80, win.restore_last_project)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

