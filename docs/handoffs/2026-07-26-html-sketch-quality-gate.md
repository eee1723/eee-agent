# HTML 草图质量门真机验收

- 日期：2026-07-26
- 分支：`feature/html-to-houdini-pipeline`
- 层级：Wave B / B1
- Provider：不需要
- Houdini：不需要

## 修复

生产 `ChromeSketchRenderer` 不再把“同名 PNG 存在”视为成功：

- 启动浏览器前删除本次目标的旧 PNG，防止陈旧结果误通过；
- 浏览器非零退出码直接失败；
- 严格校验 PNG signature、chunk 边界、CRC、IHDR、尺寸、像素格式和
  bounded zlib 解码；
- 最小尺寸为 320×200，最大文件 16 MiB、最大解码体积 64 MiB；
- 全透明、近全透明、全白或近单色结果返回 `sketch.image_blank`；
- 成功结果增加宽、高和像素通道跨度的有界证据。

## 真机 smoke

入口：

```powershell
.\.venv\Scripts\python.exe tests/runtime/sketch_chrome_smoke.py
```

本机 Chrome + pinned Three.js CDN 结果：

```text
SKETCH CHROME SMOKE OK
{"browser":"chrome.exe","image_bytes":15885,"image_height":900,
 "image_width":1440,"pixel_channel_span":231,"png_nonblank":true}
```

离线回归覆盖：

- 无浏览器；
- 非法输入；
- 浏览器超时；
- 非零退出；
- 陈旧 PNG；
- 无效 PNG；
- 空白 PNG；
- 有效非空 PNG。

## 尚未证明

- 真实 Provider 连续三次生成草图的一致性；
- 用户审核后的第二轮 Runtime 会话；
- 椅子、书桌、货架三案例的完整 Houdini commit。

当前进程没有已批准 Provider API key，这些项保持 `not_run`。
