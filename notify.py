# -*- coding: utf-8 -*-
"""Windows 桌面通知（余额不足告警用）。

三级兜底：win10toast → plyer → PowerShell 的 WinRT toast（系统自带，啥都不用装）。
全都失败就静默放弃——通知弹不出来绝不能影响主程序。整个过程丢在后台线程里跑，
因为三级兜底里有的实现会阻塞好几秒。
"""

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

def _notify(title: str, message: str) -> None:
    """弹 Windows 桌面通知（余额不足时用）。
    三级兜底，全都失败就静默放弃——通知弹不出来绝不能影响主程序。"""
    def _run():
        # ① win10toast（requirements 里的首选）
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, message, duration=8, threaded=False)
            return
        except Exception:
            pass

        # ② plyer（跨平台，装了就用）
        try:
            from plyer import notification
            notification.notify(title=title, message=message, timeout=8)
            return
        except Exception:
            pass

        # ③ PowerShell 的 WinRT toast（Windows 10/11 自带，啥都不用装）
        try:
            if sys.platform != "win32":
                return
            # 未打包时用 app 名，打包后用自己的 exe 全路径当 AppID
            app_id = str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) \
                else "Claude Code 助手"

            def _esc(s):
                """XML 转义，避免标题/正文里的特殊字符把模板弄坏"""
                return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;").replace('"', "&quot;"))

            # ★ 用字符串拼 XML 再 LoadXml，比 GetElementsByTagName + AppendChild 稳
            # （后者在 PowerShell 5.1 上会报"枚举无法执行"）
            template = (
                '<toast><visual><binding template="ToastText02">'
                f'<text id="1">{_esc(title)}</text>'
                f'<text id="2">{_esc(message)}</text>'
                "</binding></visual></toast>"
            )
            ps = (
                '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
                'ContentType=WindowsRuntime] | Out-Null\n'
                '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
                'ContentType=WindowsRuntime] | Out-Null\n'
                f'$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n'
                f'$xml.LoadXml(@\'\n{template}\n\'@)\n'
                f'$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n'
                f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
                f'"{app_id}").Show($toast)\n'
            )
            with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                             encoding="utf-8-sig") as f:
                f.write(ps)
                script = f.name
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass                    # 静默放弃

    threading.Thread(target=_run, daemon=True).start()
