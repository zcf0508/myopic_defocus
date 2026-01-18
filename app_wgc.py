#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
近视散焦 - 使用 Windows Graphics Capture API (WGC)
WGC 比 mss (GDI) 快很多，且支持 WDA_EXCLUDEFROMCAPTURE
"""

import sys
import os
import json
import math
import ctypes
from ctypes import wintypes
import numpy as np
import cv2
import threading
import queue

from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QSlider, QSpinBox, QLabel, QPushButton,
                             QDoubleSpinBox, QGroupBox, QGridLayout,
                             QSystemTrayIcon, QMenu)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QPainter, QImage, QPixmap, QIcon, QAction, QPixmap as QPixmapGui

from windows_capture import WindowsCapture, Frame, InternalCaptureControl

# Windows API
user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WDA_EXCLUDEFROMCAPTURE = 0x00000011

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long

# 配置
CONFIG_PATH = os.path.join(os.getenv('APPDATA'), 'MyopicDefocus')
CONFIG_FILE = os.path.join(CONFIG_PATH, 'config.json')
DEFAULT_CONFIG = {
    'resX': 2560, 'resY': 1440, 'diagInch': 14.0,
    'screenDistanceCM': 40, 'pupilSizeUm': 6500, 'effectStrengthPercent': 25,
}

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                c = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in c: c[k] = v
                return c
    except: pass
    return DEFAULT_CONFIG.copy()

def save_config(c):
    try:
        os.makedirs(CONFIG_PATH, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f: json.dump(c, f)
    except: pass

def calc_blur(config):
    resx, resy = config['resX'], config['resY']
    diag_mm = config['diagInch'] * 25.4
    mm_per_px = diag_mm / math.sqrt(resx**2 + resy**2)
    pix = resx * mm_per_px / resx
    screen_mm = config['screenDistanceCM'] * 10
    pupil = config['pupilSizeUm'] / 1000.0
    
    sh = 0.23
    lca_b, lca_g = 1.10 + sh, 0.24 + sh
    
    G = 1000 / (1000 / screen_mm + lca_b)
    blur_b = (pupil * (screen_mm - G) / G / pix) * 0.32
    
    G = 1000 / (1000 / screen_mm + lca_g)
    blur_g = (pupil * (screen_mm - G) / G / pix) * 0.32
    
    return blur_b, blur_g, config['effectStrengthPercent'] / 100.0


class FrameSignal(QObject):
    ready = Signal(object)


class OverlayWindow(QWidget):
    def __init__(self, geometry, scale):
        super().__init__()
        self.scale = scale
        self.logical_w = geometry.width()
        self.logical_h = geometry.height()
        self.physical_w = int(geometry.width() * scale)
        self.physical_h = int(geometry.height() * scale)
        self.pixmap = None
        
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setGeometry(geometry)
        
        self.signal = FrameSignal()
        self.signal.ready.connect(self.on_frame)
        
        config = load_config()
        blur_b, blur_g, strength = calc_blur(config)
        self.blur_b = blur_b
        self.blur_g = blur_g
        self.strength = strength
        
        self.capture_control = None
        self._frame_count = 0
        self._pending_frame = None  # 跳帧：只保留最新帧
        self._processing = False
        self._paused = False  # 暂停状态
        
    def apply_effect(self, frame):
        """Gaussian Blur on blue/green channels - GPU 加速版 (OpenCL via UMat)
        
        与 JS 版本一致：使用高斯模糊，blur_b/blur_g 直接作为 sigma (stdDeviation)
        """
        # 高斯模糊的 sigma 就是 blur 值 (与 JS feGaussianBlur stdDeviation 一致)
        sigma_b = self.blur_b
        sigma_g = self.blur_g
        
        # sigma 太小就不处理
        if sigma_b < 0.5 and sigma_g < 0.5:
            return frame
        
        # 根据 sigma 计算 kernel size (必须是奇数，且足够大以容纳高斯分布)
        # 一般取 6*sigma 或更大，向上取奇数
        def sigma_to_ksize(sigma):
            if sigma < 0.5:
                return 0
            k = int(math.ceil(sigma * 6)) | 1  # 确保是奇数
            return max(3, k)
        
        k_b = sigma_to_ksize(sigma_b)
        k_g = sigma_to_ksize(sigma_g)
        
        # 上传到 GPU
        gpu_frame = cv2.UMat(frame)
        
        # 分离通道 (在 GPU 上)
        channels = cv2.split(gpu_frame)
        b, g, r = channels[0], channels[1], channels[2]
        
        if k_b > 0 and self.strength > 0:
            b_blur = cv2.GaussianBlur(b, (k_b, k_b), sigma_b)
            b = cv2.addWeighted(b, 1 - self.strength, b_blur, self.strength, 0)
        
        if k_g > 0 and self.strength > 0:
            g_blur = cv2.GaussianBlur(g, (k_g, k_g), sigma_g)
            g = cv2.addWeighted(g, 1 - self.strength, g_blur, self.strength, 0)
        
        # 合并并下载回 CPU
        result = cv2.merge([b, g, r])
        return result.get() if isinstance(result, cv2.UMat) else result
    
    def on_wgc_frame(self, frame: Frame, capture_control: InternalCaptureControl):
        """WGC 回调 - 在捕获线程中执行，尽量快速返回"""
        try:
            # 暂停时跳过处理
            if self._paused:
                return
            # 如果正在处理，跳过这帧（保留最新的）
            if self._processing:
                return
            # frame.frame_buffer 是 BGRA numpy array，需要拷贝因为原始缓冲区会被重用
            bgra = frame.frame_buffer.copy()
            # 发送到主线程处理，避免阻塞捕获
            self.signal.ready.emit(bgra)
        except Exception as e:
            print(f"WGC frame error: {e}")
    
    def on_wgc_closed(self):
        print("WGC capture closed")
        
    def on_frame(self, bgra):
        """主线程处理帧"""
        self._processing = True
        self._frame_count += 1
        
        # BGRA -> BGR (slice, no copy)
        bgr = bgra[:, :, :3]
        
        # 应用色差效果
        processed = self.apply_effect(bgr)
        
        h, w = processed.shape[:2]
        
        if self._frame_count <= 3:
            print(f"on_frame #{self._frame_count}: bgra={bgra.shape}, processed={processed.shape}")
            print(f"  期望物理尺寸: {self.physical_w}x{self.physical_h}, 实际: {w}x{h}")
            print(f"  逻辑尺寸: {self.logical_w}x{self.logical_h}, scale: {self.scale}")
        
        # 直接用 BGR888，Qt 会正确处理
        processed = np.ascontiguousarray(processed)
        img = QImage(processed.data, w, h, w * 3, QImage.Format.Format_BGR888)
        self.pixmap = QPixmap.fromImage(img.copy())
        self.pixmap.setDevicePixelRatio(self.scale)
        self._processing = False
        self.update()
    
    def update_params(self, blur_b, blur_g, strength):
        self.blur_b = blur_b
        self.blur_g = blur_g
        self.strength = strength
    
    def pause(self):
        """暂停效果"""
        self._paused = True
        self.hide()
    
    def resume(self):
        """恢复效果"""
        self._paused = False
        self.show()
    
    def is_paused(self):
        return self._paused
    
    def start(self):
        self.setup_passthrough()
        # 延迟启动捕获，确保窗口属性已生效
        QTimer.singleShot(200, self.start_capture)
    
    def start_capture(self):
        print("Starting WGC capture...")
        
        # 创建 WGC 捕获实例 (monitor_index 从 1 开始)
        capture = WindowsCapture(
            cursor_capture=False,  # 不捕获鼠标，避免红边
            draw_border=False,
            monitor_index=1,  # 主显示器
        )
        
        # 注册事件处理器
        @capture.event
        def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
            self.on_wgc_frame(frame, capture_control)
        
        @capture.event
        def on_closed():
            self.on_wgc_closed()
        
        # 在后台线程启动捕获
        self.capture_control = capture.start_free_threaded()
        print("WGC capture started")
    
    def setup_passthrough(self):
        hwnd = int(self.winId())
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        result = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        print(f"鼠标穿透 + 排除截屏 已设置 (result={result})")
    
    def paintEvent(self, event):
        if self.pixmap:
            p = QPainter(self)
            # 直接绘制在 (0,0)，Qt 会根据 devicePixelRatio 自动 1:1 映射
            p.drawPixmap(0, 0, self.pixmap)
    
    def closeEvent(self, e):
        if self.capture_control:
            self.capture_control.stop()
        e.accept()


class ControlWindow(QWidget):
    def __init__(self, overlay):
        super().__init__()
        self.overlay = overlay
        self.tray = None  # 稍后设置
        self.config = load_config()
        self.setWindowTitle("近视散焦 - WGC版")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout()
        
        g1 = QGroupBox("屏幕参数")
        l1 = QGridLayout()
        self.res_x = QSpinBox(); self.res_x.setRange(800,7680); self.res_x.setValue(self.config['resX'])
        self.res_y = QSpinBox(); self.res_y.setRange(600,4320); self.res_y.setValue(self.config['resY'])
        self.diag = QDoubleSpinBox(); self.diag.setRange(10,100); self.diag.setValue(self.config['diagInch'])
        self.dist = QSpinBox(); self.dist.setRange(20,200); self.dist.setValue(self.config['screenDistanceCM'])
        l1.addWidget(QLabel("分辨率宽:"),0,0); l1.addWidget(self.res_x,0,1)
        l1.addWidget(QLabel("分辨率高:"),1,0); l1.addWidget(self.res_y,1,1)
        l1.addWidget(QLabel("对角线(寸):"),2,0); l1.addWidget(self.diag,2,1)
        l1.addWidget(QLabel("视距(cm):"),3,0); l1.addWidget(self.dist,3,1)
        g1.setLayout(l1); layout.addWidget(g1)
        
        g2 = QGroupBox("效果参数")
        l2 = QGridLayout()
        self.pupil = QSpinBox(); self.pupil.setRange(2000,9000); self.pupil.setValue(self.config['pupilSizeUm'])
        self.strength = QSlider(Qt.Horizontal); self.strength.setRange(0,100); self.strength.setValue(self.config['effectStrengthPercent'])
        self.strength_lbl = QLabel(f"{self.config['effectStrengthPercent']}%")
        self.strength.valueChanged.connect(lambda v: self.strength_lbl.setText(f"{v}%"))
        self.strength.valueChanged.connect(self.apply)
        l2.addWidget(QLabel("瞳孔(μm):"),0,0); l2.addWidget(self.pupil,0,1)
        h = QHBoxLayout(); h.addWidget(self.strength); h.addWidget(self.strength_lbl)
        l2.addWidget(QLabel("强度:"),1,0); l2.addLayout(h,1,1)
        g2.setLayout(l2); layout.addWidget(g2)
        
        g3 = QGroupBox("模糊值 (只读)")
        l3 = QGridLayout()
        self.blur_b_lbl = QLabel(); self.blur_g_lbl = QLabel()
        l3.addWidget(QLabel("蓝通道:"),0,0); l3.addWidget(self.blur_b_lbl,0,1)
        l3.addWidget(QLabel("绿通道:"),1,0); l3.addWidget(self.blur_g_lbl,1,1)
        g3.setLayout(l3); layout.addWidget(g3)
        
        btns = QHBoxLayout()
        b1 = QPushButton("应用"); b1.clicked.connect(self.apply)
        b2 = QPushButton("保存默认"); b2.clicked.connect(self.save)
        self.pause_btn = QPushButton("暂停"); self.pause_btn.clicked.connect(self.toggle_pause)
        b3 = QPushButton("退出"); b3.clicked.connect(QApplication.quit)
        btns.addWidget(b1); btns.addWidget(b2); btns.addWidget(self.pause_btn); btns.addWidget(b3)
        layout.addLayout(btns)
        
        self.setLayout(layout)
        self.apply()
        
        for w in [self.res_x, self.res_y, self.dist, self.pupil]:
            w.valueChanged.connect(self.apply)
        self.diag.valueChanged.connect(self.apply)
    
    def apply(self):
        c = self.get_config()
        blur_b, blur_g, strength = calc_blur(c)
        self.blur_b_lbl.setText(f"{blur_b:.2f} px")
        self.blur_g_lbl.setText(f"{blur_g:.2f} px")
        self.overlay.update_params(blur_b, blur_g, strength)
    
    def get_config(self):
        return {
            'resX': self.res_x.value(), 'resY': self.res_y.value(),
            'diagInch': self.diag.value(), 'screenDistanceCM': self.dist.value(),
            'pupilSizeUm': self.pupil.value(), 'effectStrengthPercent': self.strength.value()
        }
    
    def save(self):
        save_config(self.get_config())
    
    def toggle_pause(self):
        if self.overlay.is_paused():
            self.overlay.resume()
            self.pause_btn.setText("暂停")
        else:
            self.overlay.pause()
            self.pause_btn.setText("启动")
        # 同步托盘菜单状态
        if self.tray:
            self.tray.update_pause_action()
    
    def update_pause_btn(self):
        """同步按钮状态"""
        if self.overlay.is_paused():
            self.pause_btn.setText("启动")
        else:
            self.pause_btn.setText("暂停")
    
    def closeEvent(self, e):
        # 关闭时隐藏到托盘，不退出程序
        e.ignore()
        self.hide()


class TrayIcon(QSystemTrayIcon):
    """系统托盘图标"""
    def __init__(self, app, control, overlay):
        # 创建一个简单的图标 (蓝色圆形)
        icon_size = 64
        icon_img = np.zeros((icon_size, icon_size, 4), dtype=np.uint8)
        cv2.circle(icon_img, (32, 32), 28, (255, 150, 50, 255), -1)  # BGRA 蓝色
        cv2.circle(icon_img, (32, 32), 28, (255, 200, 100, 255), 2)  # 边框
        
        qimg = QImage(icon_img.data, icon_size, icon_size, icon_size * 4, QImage.Format.Format_RGBA8888)
        icon = QIcon(QPixmap.fromImage(qimg))
        
        super().__init__(icon, app)
        self.app = app
        self.control = control
        self.overlay = overlay
        self.setToolTip("近视散焦 - WGC版")
        
        # 创建托盘菜单
        self.menu = QMenu()
        
        self.show_action = self.menu.addAction("打开设置")
        self.show_action.triggered.connect(self.show_settings)
        
        self.menu.addSeparator()
        
        self.pause_action = self.menu.addAction("暂停")
        self.pause_action.triggered.connect(self.toggle_pause)
        
        self.menu.addSeparator()
        
        self.quit_action = self.menu.addAction("退出程序")
        self.quit_action.triggered.connect(self.do_quit)
        
        self.setContextMenu(self.menu)
        
        # 双击托盘图标打开设置
        self.activated.connect(self.on_activated)
        
        self.show()
    
    def show_settings(self):
        self.control.show()
        self.control.raise_()
        self.control.activateWindow()
    
    def toggle_pause(self):
        if self.overlay.is_paused():
            self.overlay.resume()
            self.pause_action.setText("暂停")
        else:
            self.overlay.pause()
            self.pause_action.setText("启动")
        # 同步设置窗口的按钮状态
        self.control.update_pause_btn()
    
    def update_pause_action(self):
        """同步菜单状态"""
        if self.overlay.is_paused():
            self.pause_action.setText("启动")
        else:
            self.pause_action.setText("暂停")
    
    def do_quit(self):
        self.overlay.close()
        self.app.quit()
    
    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_settings()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    geo = screen.geometry()
    scale = screen.devicePixelRatio()
    fps = screen.refreshRate()
    
    print(f"屏幕: {geo.width()}x{geo.height()}, 缩放: {scale}, 刷新率: {fps}Hz")
    print("使用 Windows Graphics Capture API")
    
    overlay = OverlayWindow(geo, scale)
    control = ControlWindow(overlay)
    
    # 创建系统托盘
    tray = TrayIcon(app, control, overlay)
    control.tray = tray  # 互相引用以同步状态
    
    overlay.show()
    overlay.start()
    control.show()
    
    sys.exit(app.exec())
