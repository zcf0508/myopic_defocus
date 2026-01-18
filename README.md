# 近视散焦 (Myopic Defocus)

基于 [Refractify](https://refractify.io/) 原理的 Windows 桌面近视散焦效果工具。

## 原理

研究表明，近视（近视眼）部分由长时间在室内或近距离屏幕工作引起。人眼存在纵向色差（Longitudinal Chromatic Aberration, LCA），通过模拟远距离光线的色差特性，可以在视网膜上产生"远视"的视觉信号，可能有助于减缓近视进展。

本项目通过对屏幕蓝色和绿色通道应用不同程度的高斯模糊，模拟 LCA 效应产生的近视散焦效果。

> **注意**：本软件仅供学习和实验用途，不构成医疗建议。如有视力问题请咨询专业眼科医生。

## 功能

- 🖥️ 使用 Windows Graphics Capture API 实现高性能屏幕捕获
- 🎨 基于 OpenCV GPU 加速的实时色差模糊效果
- ⚙️ 可调节屏幕参数（分辨率、对角线尺寸、视距）
- 👁️ 可调节瞳孔大小和效果强度
- ⏸️ 支持暂停/启动效果
- 📌 系统托盘常驻，最小化到托盘运行

## 系统要求

- Windows 10/11
- Python 3.10+

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
python app_wgc.py
```

启动后会显示设置窗口和全屏覆盖层。可通过设置窗口调整参数，或通过系统托盘菜单控制暂停/启动。

## 参数说明

| 参数 | 说明 |
|------|------|
| 分辨率宽/高 | 屏幕分辨率 |
| 对角线(寸) | 屏幕对角线尺寸（英寸） |
| 视距(cm) | 眼睛到屏幕的距离 |
| 瞳孔(μm) | 瞳孔直径（微米），影响模糊程度 |
| 强度 | 效果混合强度 (0-100%) |

## 致谢

- [Refractify](https://refractify.io/) - 近视散焦技术原理和浏览器插件
- [windows-capture](https://github.com/NiiightmareXD/windows-capture) - Windows Graphics Capture API Python 绑定

## 许可

MIT License
