# -*- coding: utf-8 -*-
"""VSCode 探测 / 安装 / 打开的离线测试。

不碰真的 VSCode，也不跑 winget / brew：候选路径用临时目录伪造，
子进程用打桩，注册表读取直接替换成固定返回。真机上最容易出错的三件事都在这里兜住：

  1. 注册表能给路径 → 必须优先于 PATH（本机 VSCode 就装在 D:\\vs code 这种非默认目录）
  2. CLI 用 bin\\code.cmd、开窗用 Code.exe（Code.exe --list-extensions 是空输出）
  3. 子进程环境里不能有 ELECTRON_RUN_AS_NODE（有它 Code.exe 会当 node 跑完就退）
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from platform_support import common                          # noqa: E402
from platform_support import linux as linux_backend          # noqa: E402
from platform_support import macos as macos_backend          # noqa: E402
from platform_support import windows as win_backend          # noqa: E402


# ================= 纯函数 =================

class ExeFromCommandTests(unittest.TestCase):
    def test_quoted_with_percent_one(self):
        self.assertEqual(
            common.exe_from_command(r'"D:\vs code\Microsoft VS Code\Code.exe" "%1"'),
            r"D:\vs code\Microsoft VS Code\Code.exe",
        )

    def test_quoted_with_open_url(self):
        self.assertEqual(
            common.exe_from_command(r'"C:\Apps\Code.exe" --open-url -- "%1"'),
            r"C:\Apps\Code.exe",
        )

    def test_display_icon_suffix(self):
        # DisplayIcon 的值常带图标序号：...,0
        self.assertEqual(
            common.exe_from_command(r"C:\Apps\Microsoft VS Code\Code.exe,0"),
            r"C:\Apps\Microsoft VS Code\Code.exe",
        )

    def test_unquoted(self):
        self.assertEqual(
            common.exe_from_command(r"C:\VSCode\Code.exe -n %1"),
            r"C:\VSCode\Code.exe",
        )

    def test_plain_path(self):
        self.assertEqual(
            common.exe_from_command(r"C:\Program Files\Microsoft VS Code\Code.exe"),
            r"C:\Program Files\Microsoft VS Code\Code.exe",
        )

    def test_empty_and_garbage(self):
        self.assertEqual(common.exe_from_command(""), "")
        self.assertEqual(common.exe_from_command("   "), "")
        self.assertEqual(common.exe_from_command('"unterminated'), "")


class ParseExtensionListTests(unittest.TestCase):
    def test_with_versions(self):
        text = "anthropic.claude-code@2.1.300\nms-python.python@2024.1.0\n"
        self.assertEqual(common.parse_extension_list(text), {
            "anthropic.claude-code": "2.1.300",
            "ms-python.python": "2024.1.0",
        })

    def test_without_versions_and_blank_lines(self):
        text = "\nAnthropic.Claude-Code\n\nms-python.python\n"
        self.assertEqual(set(common.parse_extension_list(text)),
                         {"anthropic.claude-code", "ms-python.python"})

    def test_empty_output(self):
        # 真机上 Code.exe --list-extensions 就是空输出，不能当"有扩展"也不能崩
        self.assertEqual(common.parse_extension_list(""), {})
        self.assertEqual(common.parse_extension_list(None), {})


class CleanEnvTests(unittest.TestCase):
    def test_drops_electron_and_vscode_vars(self):
        src = {
            "PATH": r"C:\Windows",
            "ELECTRON_RUN_AS_NODE": "1",
            "VSCODE_PID": "1234",
            "VSCODE_IPC_HOOK_CLI": r"\\.\pipe\x",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "electron_run_as_node": "1",          # 大小写不一样也得拦
            "HOME": r"C:\Users\x",
        }
        out = common.clean_env(src)
        self.assertEqual(set(out), {"PATH", "HOME"})
        # 入参不能被改
        self.assertIn("ELECTRON_RUN_AS_NODE", src)

    def test_keeps_unrelated_vars(self):
        out = common.clean_env({"MY_VAR": "1"})
        self.assertEqual(out, {"MY_VAR": "1"})

    def test_from_os_environ_by_default(self):
        with mock.patch.dict(os.environ, {"ELECTRON_RUN_AS_NODE": "1"}, clear=False):
            self.assertNotIn("ELECTRON_RUN_AS_NODE", common.clean_env())


class SmartDecodeTests(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(common.smart_decode("插件装好了".encode("utf-8")), "插件装好了")

    def test_gbk_fallback(self):
        raw = "不是内部或外部命令".encode("gbk")
        # 在 UTF-8 系统上 GBK 字节解出来会有替换字符，兜底结果不能抛异常
        self.assertIsInstance(common.smart_decode(raw), str)

    def test_empty(self):
        self.assertEqual(common.smart_decode(b""), "")


class DownloadPageFallbackTests(unittest.TestCase):
    def test_browser_opened(self):
        with mock.patch.object(common, "open_url", return_value=True):
            text = common.download_page_fallback("没有 winget")
        self.assertTrue(text.startswith("没能自动装 VSCode"))
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)
        self.assertIn("没有 winget", text)

    def test_browser_failed(self):
        with mock.patch.object(common, "open_url", return_value=False):
            text = common.download_page_fallback("没有 winget")
        self.assertIn("也没能自动打开浏览器", text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)


# ================= Windows 后端 =================

def _make_vscode_tree(root: Path, with_cli: bool = True) -> Path:
    """在临时目录里伪造一份 VSCode 安装目录，返回 Code.exe 的路径。"""
    exe = root / "Microsoft VS Code" / "Code.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    if with_cli:
        cli = exe.parent / "bin" / "code.cmd"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("@echo off\n", encoding="utf-8")
    return exe


class WindowsFindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.exe = _make_vscode_tree(self.tmp)

    def test_registry_candidate_wins(self):
        with mock.patch.object(win_backend, "_vscode_registry_candidates",
                               return_value=[str(self.exe)]), \
             mock.patch.object(win_backend, "_vscode_standard_paths", return_value=[]), \
             mock.patch.object(win_backend, "_vscode_uninstall_candidates", return_value=[]), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            info = win_backend.find_vscode()

        self.assertIsNotNone(info)
        self.assertEqual(info["source"], "registry")
        self.assertEqual(info["exe"], str(self.exe))
        self.assertEqual(info["launch_argv"], [str(self.exe)])
        # CLI 必须是 bin\code.cmd，不能是 Code.exe
        self.assertEqual(info["cli_argv"],
                         [str(self.exe.parent / "bin" / "code.cmd")])

    def test_missing_registry_path_falls_through_to_which(self):
        other = _make_vscode_tree(self.tmp / "other")
        with mock.patch.object(win_backend, "_vscode_registry_candidates",
                               return_value=[str(self.tmp / "gone" / "Code.exe")]), \
             mock.patch.object(win_backend, "_vscode_standard_paths", return_value=[]), \
             mock.patch.object(win_backend, "_vscode_uninstall_candidates", return_value=[]), \
             mock.patch.object(win_backend.shutil, "which",
                               return_value=str(other / "bin" / "code.cmd")):
            info = win_backend.find_vscode()

        self.assertIsNotNone(info)
        self.assertEqual(info["source"], "PATH")
        # 从 code.cmd 反推回安装根目录
        self.assertEqual(info["exe"], str(other))

    def test_nothing_found(self):
        with mock.patch.object(win_backend, "_vscode_registry_candidates", return_value=[]), \
             mock.patch.object(win_backend, "_vscode_standard_paths", return_value=[]), \
             mock.patch.object(win_backend, "_vscode_uninstall_candidates", return_value=[]), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertIsNone(win_backend.find_vscode())

    def test_no_cli_means_empty_cli_argv(self):
        exe = _make_vscode_tree(self.tmp / "nobin", with_cli=False)
        info = win_backend._vscode_info(exe, "test")
        self.assertEqual(info["cli_argv"], [])
        # 没 CLI 也要能打开
        self.assertEqual(info["launch_argv"], [str(exe)])


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        self.winget = self.tmp / "winget.exe"
        self.winget.write_bytes(b"")

    def _patches(self, run_result=(b"", b"", False, 0), found=None):
        return [
            mock.patch.object(win_backend, "HOME", self.fake_home),
            mock.patch.object(win_backend, "find_vscode", return_value=found),
            mock.patch.object(win_backend.shutil, "which",
                              side_effect=lambda n: str(self.winget)
                              if n in ("winget", "winget.exe") else None),
            mock.patch.object(win_backend, "run_capture", return_value=run_result),
            mock.patch.object(common, "open_url", return_value=True),
            mock.patch.object(win_backend.time, "sleep"),
        ]

    def test_already_installed(self):
        info = {"exe": r"D:\vs code\Code.exe"}
        with mock.patch.object(win_backend, "find_vscode", return_value=info), \
             mock.patch.object(win_backend, "run_capture") as run:
            text = win_backend.install_vscode()
        self.assertTrue(text.startswith("已经装过 VSCode"))
        run.assert_not_called()                    # 别重复装

    def test_argv_and_success(self):
        info = {"exe": r"D:\vs code\Microsoft VS Code\Code.exe"}
        patches = self._patches(found=None)
        # 装完后重探才找得到
        patches[1] = mock.patch.object(win_backend, "find_vscode",
                                       side_effect=[None, info])
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        text = win_backend.install_vscode()

        self.assertEqual(text, f"已安装 VSCode（{info['exe']}）")
        argv = win_backend.run_capture.call_args.args[0]
        self.assertEqual(argv, [
            str(self.winget), "install", "-e",
            "--id", "Microsoft.VisualStudioCode",
            "--scope", "user",                  # ★ 用户级，不弹 UAC
            "--accept-package-agreements", "--accept-source-agreements",
        ])
        kwargs = win_backend.run_capture.call_args.kwargs
        self.assertEqual(kwargs["timeout"], win_backend.VSCODE_INSTALL_TIMEOUT)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        # 环境里不能有 ELECTRON_RUN_AS_NODE
        self.assertNotIn("ELECTRON_RUN_AS_NODE", kwargs["env"])

    def test_nonzero_exit_falls_back_to_download_page(self):
        for p in self._patches(run_result=(b"", "boom".encode(), False, 1)):
            p.start()
            self.addCleanup(p.stop)

        text = win_backend.install_vscode()

        self.assertTrue(text.startswith("没能自动装 VSCode"), text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)
        self.assertIn("winget 返回 1", text)

    def test_timeout_falls_back(self):
        for p in self._patches(run_result=(b"", b"", True, -1)):
            p.start()
            self.addCleanup(p.stop)

        text = win_backend.install_vscode()

        self.assertTrue(text.startswith("没能自动装 VSCode"), text)
        self.assertIn("900 秒", text)

    def test_no_winget_falls_back(self):
        """连 WindowsApps 的兜底路径也没有、自动装 winget 也失败 → 打开下载页。

        （自动装 winget 本身在 tests/test_winget.py 里测；这里只关心它失败之后。）"""
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)
        # 连 WindowsApps 里的兜底路径也不存在
        with mock.patch.object(win_backend.shutil, "which", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget",
                               return_value={"ok": False, "exe": "", "mode": "download",
                                             "note": "下载 winget 失败（离线）"}):
            text = win_backend.install_vscode()
        self.assertIn("下载 winget 失败", text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)

    def test_installed_but_not_found(self):
        for p in self._patches():
            p.start()
            self.addCleanup(p.stop)

        text = win_backend.install_vscode()

        # 说成功就得真找得到，找不到要如实说
        self.assertTrue(text.startswith("没能自动装 VSCode"), text)
        self.assertIn("找不到", text)


class LaunchArgvTests(unittest.TestCase):
    """"打开目录"和"开新窗口再打开目录"是两种 argv，别拼错。"""

    def test_reuse_uses_launch_argv(self):
        info = {"launch_argv": [r"D:\vs code\Code.exe"], "cli_argv": [r"D:\vs code\bin\code.cmd"]}
        self.assertEqual(common.launch_argv_for(info, False, r"C:\work"),
                         [r"D:\vs code\Code.exe", r"C:\work"])

    def test_new_window_uses_cli(self):
        # ★ 必须走 CLI：Code.exe 收到参数是转手给已运行实例的，-n 不生效
        info = {"launch_argv": [r"D:\vs code\Code.exe"], "cli_argv": [r"D:\vs code\bin\code.cmd"]}
        self.assertEqual(common.launch_argv_for(info, True, r"C:\work"),
                         [r"D:\vs code\bin\code.cmd", "-n", r"C:\work"])

    def test_new_window_without_cli_falls_back(self):
        info = {"launch_argv": ["/usr/share/code/code"], "cli_argv": []}
        self.assertEqual(common.launch_argv_for(info, True, "/home/x"),
                         ["/usr/share/code/code", "/home/x"])


class WindowsOpenTests(unittest.TestCase):
    def _run(self, info, directory="", new_window=False):
        with mock.patch.object(win_backend, "find_vscode", return_value=info), \
             mock.patch.object(win_backend.subprocess, "Popen") as popen, \
             mock.patch.dict(os.environ, {"ELECTRON_RUN_AS_NODE": "1"}, clear=False):
            text = win_backend.open_vscode(directory, new_window)
        return text, popen

    def test_prefix_and_argv(self):
        info = {"exe": r"D:\vs code\Code.exe", "launch_argv": [r"D:\vs code\Code.exe"],
                "cli_argv": []}
        text, popen = self._run(info)

        self.assertTrue(text.startswith("已打开 VSCode"), text)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], r"D:\vs code\Code.exe")
        self.assertEqual(len(argv), 2)
        self.assertEqual(Path(argv[1]), Path(win_backend.HOME))

    def test_env_is_cleaned(self):
        info = {"exe": "x", "launch_argv": ["x"], "cli_argv": []}
        _text, popen = self._run(info)
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("ELECTRON_RUN_AS_NODE", env)

    def test_new_window_goes_through_cli(self):
        info = {"exe": r"D:\vs code\Code.exe",
                "launch_argv": [r"D:\vs code\Code.exe"],
                "cli_argv": [r"D:\vs code\bin\code.cmd"]}
        _text, popen = self._run(info, new_window=True)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:2], [r"D:\vs code\bin\code.cmd", "-n"])

    def test_open_uri_uses_shell_execute(self):
        with mock.patch.object(win_backend.os, "startfile", create=True) as startfile:
            self.assertEqual(win_backend.open_uri("vscode://anthropic.claude-code/open"), "")
        startfile.assert_called_once_with("vscode://anthropic.claude-code/open")

    def test_open_uri_failure_is_reported(self):
        with mock.patch.object(win_backend.os, "startfile", create=True,
                               side_effect=OSError("没有关联的程序")):
            msg = win_backend.open_uri("vscode://x/y")
        self.assertIn("打不开链接", msg)

    def test_not_installed_prefix_does_not_claim_success(self):
        text, popen = self._run(None)
        self.assertFalse(text.startswith("已打开 VSCode"))
        self.assertIn("找不到 VSCode", text)
        popen.assert_not_called()

    def test_popen_error_reported(self):
        info = {"exe": "x", "launch_argv": ["x"], "cli_argv": []}
        with mock.patch.object(win_backend, "find_vscode", return_value=info), \
             mock.patch.object(win_backend.subprocess, "Popen", side_effect=OSError("nope")):
            text = win_backend.open_vscode("")
        self.assertTrue(text.startswith("打开 VSCode 失败"), text)


# ================= macOS / Linux 后端 =================

class LinuxBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.cli = self.tmp / "code"
        self.cli.write_text("#!/bin/sh\n", encoding="utf-8")

    def test_find_uses_cli(self):
        with mock.patch.object(linux_backend, "_vscode_cli_candidates",
                               return_value=[str(self.cli)]):
            info = linux_backend.find_vscode()
        self.assertEqual(info["exe"], str(self.cli))
        self.assertEqual(info["cli_argv"], [str(self.cli)])
        self.assertEqual(info["launch_argv"], [str(self.cli)])

    def test_find_nothing(self):
        with mock.patch.object(linux_backend, "_vscode_cli_candidates",
                               return_value=[]), \
             mock.patch.object(linux_backend, "_flatpak_vscode_argv", return_value=[]):
            self.assertIsNone(linux_backend.find_vscode())

    def test_flatpak(self):
        with mock.patch.object(linux_backend, "_vscode_cli_candidates", return_value=[]), \
             mock.patch.object(linux_backend, "_flatpak_vscode_argv",
                               return_value=["flatpak", "run", "com.visualstudio.code"]):
            info = linux_backend.find_vscode()
        self.assertEqual(info["source"], "flatpak")
        self.assertEqual(info["cli_argv"], ["flatpak", "run", "com.visualstudio.code"])

    def test_install_never_elevates(self):
        # Linux 上装 VSCode 要 sudo，助手只开下载页 —— 绝不能偷偷跑 apt/snap
        with mock.patch.object(linux_backend, "find_vscode", return_value=None), \
             mock.patch.object(linux_backend, "run_capture") as run, \
             mock.patch.object(common, "open_url", return_value=True):
            text = linux_backend.install_vscode()
        run.assert_not_called()
        self.assertIn("管理员权限", text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)

    def test_install_reports_existing(self):
        with mock.patch.object(linux_backend, "find_vscode",
                               return_value={"exe": "/snap/bin/code"}):
            text = linux_backend.install_vscode()
        self.assertTrue(text.startswith("已经装过 VSCode"), text)

    def test_open_prefix(self):
        info = {"exe": "/snap/bin/code", "launch_argv": ["/snap/bin/code"], "cli_argv": []}
        with mock.patch.object(linux_backend, "find_vscode", return_value=info), \
             mock.patch.object(linux_backend.subprocess, "Popen") as popen:
            text = linux_backend.open_vscode("")
        self.assertTrue(text.startswith("已打开 VSCode"), text)
        self.assertEqual(popen.call_args.args[0][0], "/snap/bin/code")
        self.assertNotIn("ELECTRON_RUN_AS_NODE", popen.call_args.kwargs["env"])


class MacBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.app = self.tmp / macos_backend.VSCODE_APP_NAME
        bin_dir = self.app / "Contents" / "Resources" / "app" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "code").write_text("#!/bin/sh\n", encoding="utf-8")

    def test_find_app_bundle(self):
        with mock.patch.object(macos_backend, "_vscode_app_candidates",
                               return_value=[self.app]):
            info = macos_backend.find_vscode()
        self.assertEqual(info["exe"], str(self.app))
        self.assertEqual(info["launch_argv"], ["open", "-a", str(self.app)])
        # 目录里有 code 脚本 → CLI 指向它（没有执行位时退化用 /bin/sh，见下一个断言）
        cli = self.app / "Contents" / "Resources" / "app" / "bin" / "code"
        self.assertEqual(info["cli_argv"][-1], str(cli))
        self.assertIn(info["cli_argv"][0], (str(cli), "/bin/sh"))

    def test_cli_without_exec_bit_uses_sh(self):
        # 手工解压 / 剪贴板拷贝来的 app 常常丢执行位，直接跑会 PermissionError
        cli = self.app / "Contents" / "Resources" / "app" / "bin" / "code"
        with mock.patch.object(macos_backend.os, "access", return_value=False):
            info = macos_backend._vscode_info(self.app)
        self.assertEqual(info["cli_argv"], ["/bin/sh", str(cli)])

    def test_find_nothing(self):
        with mock.patch.object(macos_backend, "_vscode_app_candidates",
                               return_value=[self.tmp / "nope.app"]), \
             mock.patch.object(macos_backend.shutil, "which", return_value=None):
            self.assertIsNone(macos_backend.find_vscode())

    def test_no_brew_falls_back(self):
        with mock.patch.object(macos_backend, "find_vscode", return_value=None), \
             mock.patch.object(macos_backend, "_find_brew", return_value=""), \
             mock.patch.object(common, "open_url", return_value=True):
            text = macos_backend.install_vscode()
        self.assertIn("没有 Homebrew", text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)

    def test_brew_argv_and_success(self):
        info = {"exe": str(self.app)}
        with mock.patch.object(macos_backend, "find_vscode",
                               side_effect=[None, info]) as find, \
             mock.patch.object(macos_backend, "_find_brew", return_value="/opt/homebrew/bin/brew"), \
             mock.patch.object(macos_backend, "run_capture",
                               return_value=(b"", b"", False, 0)) as run:
            text = macos_backend.install_vscode()

        self.assertEqual(text, f"已安装 VSCode（{info['exe']}）")
        self.assertEqual(run.call_args.args[0],
                         ["/opt/homebrew/bin/brew", "install", "--cask", "visual-studio-code"])
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)   # 不能等 sudo 回车
        self.assertEqual(kwargs["env"].get("NONINTERACTIVE"), "1")
        self.assertNotIn("ELECTRON_RUN_AS_NODE", kwargs["env"])

    def test_open_prefix(self):
        info = {"exe": str(self.app), "launch_argv": ["open", "-a", str(self.app)],
                "cli_argv": []}
        with mock.patch.object(macos_backend, "find_vscode", return_value=info), \
             mock.patch.object(macos_backend.subprocess, "Popen") as popen:
            text = macos_backend.open_vscode("")
        self.assertTrue(text.startswith("已打开 VSCode"), text)
        self.assertEqual(popen.call_args.args[0][:3], ["open", "-a", str(self.app)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
