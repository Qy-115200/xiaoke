# -*- coding: utf-8 -*-
"""没 winget 时自动把它装上 —— 全离线，一个字节都不下载。

背景（用户 2026-10-30 遇到的那次）：装 VSCode 走到一半，报「这台机器上没有 winget」，
流程就断了。所以现在没 winget 会先下微软官方的 App Installer（x64 那个 msix），
装完再用 winget 装 VSCode。

这里钉住的行为：
  · 本来就有 winget 就一次网络都不走；
  · 下的包必须过 sha256，不对就删掉重来，绝不拿去装进系统；
  · AppX 装不上要退到便携版（解压即用），不能直接放弃；
  · 装箱的临时文件用完要删掉（58MB）；
  · 哪一步失败都返回一句人话，不抛异常、不留半截状态。
"""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from platform_support import common  # noqa: E402
from platform_support import windows as win_backend  # noqa: E402


class _FakeResp:
    """假的 requests 响应：按块吐，size 可控。"""

    def __init__(self, payload: bytes, status: int = 200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers if headers is not None else {
            "content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]


def _fake_requests(resp):
    mod = mock.Mock()
    mod.get = mock.Mock(return_value=resp)
    return mod


class FindWingetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_path_first(self):
        exe = self.tmp / "winget.exe"
        exe.write_bytes(b"")
        with mock.patch.object(win_backend.shutil, "which",
                               side_effect=lambda n: str(exe) if n == "winget" else None):
            self.assertEqual(win_backend.find_winget(), str(exe))

    def test_windowsapps_alias_is_used_even_without_path(self):
        """刚装完 App Installer，PATH 还是旧的 —— 别名文件已经在磁盘上了。"""
        alias = self.tmp / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe"
        alias.parent.mkdir(parents=True)
        alias.write_bytes(b"")
        with mock.patch.object(win_backend, "HOME", self.tmp), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertEqual(win_backend.find_winget(), str(alias))

    def test_portable_copy_is_remembered(self):
        """便携版解压到本地目录后，下次直接用它，不重复下载 58MB。"""
        portable = self.tmp / WINGET_PORTABLE_VERSION / "winget.exe"
        portable.parent.mkdir(parents=True)
        portable.write_bytes(b"")
        with mock.patch.object(win_backend, "_winget_portable_dir",
                               return_value=portable.parent), \
             mock.patch.object(win_backend, "HOME", self.tmp / "nohome"), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertEqual(win_backend.find_winget(), str(portable))

    def test_nothing_found(self):
        with mock.patch.object(win_backend, "HOME", self.tmp / "nohome"), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertIsNone(win_backend.find_winget())


WINGET_PORTABLE_VERSION = win_backend.WINGET_VERSION


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.dest = self.tmp / "x.msix"

    def _run(self, payload, status=200, headers=None):
        fake = _fake_requests(_FakeResp(payload, status, headers))
        with mock.patch.dict(sys.modules, {"requests": fake}):
            return win_backend._download("https://example.invalid/x", self.dest)

    def test_good_file_is_kept(self):
        payload = b"hello winget"
        with mock.patch.object(win_backend, "WINGET_MSIX_SIZE", len(payload)), \
             mock.patch.object(win_backend, "WINGET_MSIX_SHA256",
                               __import__("hashlib").sha256(payload).hexdigest()):
            self.assertEqual(self._run(payload), "")
        self.assertTrue(self.dest.is_file())

    def test_hash_mismatch_keeps_nothing(self):
        payload = b"hello winget"
        with mock.patch.object(win_backend, "WINGET_MSIX_SIZE", len(payload)), \
             mock.patch.object(win_backend, "WINGET_MSIX_SHA256", "0" * 64):
            why = self._run(payload)
        self.assertIn("校验不过", why)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".msix.part").exists())

    def test_wrong_size_is_rejected_before_hashing(self):
        with mock.patch.object(win_backend, "WINGET_MSIX_SIZE", 999):
            why = self._run(b"abc", headers={"content-length": "3"})
        self.assertIn("大小不对", why)
        self.assertFalse(self.dest.exists())

    def test_http_error_is_reported(self):
        why = self._run(b"nope", status=404)
        self.assertIn("HTTP 404", why)

    def test_progress_callback_sees_bytes(self):
        seen = []
        payload = b"x" * 100
        fake = _fake_requests(_FakeResp(payload))
        with mock.patch.dict(sys.modules, {"requests": fake}), \
             mock.patch.object(win_backend, "WINGET_MSIX_SIZE", 100), \
             mock.patch.object(win_backend, "WINGET_MSIX_SHA256",
                               __import__("hashlib").sha256(payload).hexdigest()):
            win_backend._download("https://example.invalid/x", self.dest,
                                  lambda done, total, what: seen.append((done, total, what)))
        self.assertEqual(seen[-1], (100, 100, "winget"))


class EnsureWingetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.msix = self.tmp / "AppInstaller_x64.msix"
        self.msix.write_bytes(b"fake-msix")
        # 依赖包默认"已经下过"（空列表）：别的用例不该因为要下 12MB 而走网络
        patcher = mock.patch.object(win_backend, "_fetch_winget_deps",
                                    return_value=([], ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_existing_short_circuits(self):
        with mock.patch.object(win_backend, "find_winget", return_value=r"C:\w\winget.exe"), \
             mock.patch.object(win_backend, "_fetch_winget_msix") as fetch:
            got = win_backend.ensure_winget()
        fetch.assert_not_called()
        self.assertEqual(got["mode"], "existing")
        self.assertTrue(got["ok"])

    def test_download_failure_is_not_an_exception(self):
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(None, "dl.aikaui.com：HTTP 500")):
            got = win_backend.ensure_winget()
        self.assertFalse(got["ok"])
        self.assertIn("下载 winget 失败", got["note"])
        self.assertIn("HTTP 500", got["note"])

    def test_appx_path_returns_the_new_exe(self):
        alias = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\winget.exe"
        with mock.patch.object(win_backend, "find_winget", side_effect=[None, alias]), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx", return_value="") as appx, \
             mock.patch.object(win_backend, "_extract_portable") as portable:
            got = win_backend.ensure_winget()
        appx.assert_called_once()
        portable.assert_not_called()
        self.assertEqual(got["mode"], "appx")
        self.assertEqual(got["exe"], alias)
        self.assertFalse(self.msix.exists(), "58MB 的安装包用完要删掉")

    def test_appx_failure_falls_back_to_portable(self):
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx",
                               return_value="Add-AppxPackage 返回 1：0x80073CFF"), \
             mock.patch.object(win_backend, "_extract_portable",
                               return_value=(r"C:\p\winget.exe", "")):
            got = win_backend.ensure_winget()
        self.assertTrue(got["ok"])
        self.assertEqual(got["mode"], "portable")
        self.assertEqual(got["exe"], r"C:\p\winget.exe")

    def test_both_fail_reports_both_reasons(self):
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx", return_value="AppX 被禁"), \
             mock.patch.object(win_backend, "_extract_portable",
                               return_value=(None, "解压后没有 winget.exe")):
            got = win_backend.ensure_winget()
        self.assertFalse(got["ok"])
        self.assertIn("AppX 被禁", got["note"])
        self.assertIn("解压后没有 winget.exe", got["note"])

    def test_install_appx_runs_powershell_with_the_path(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return b"", b"", False, 0

        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            self.assertEqual(win_backend._install_appx(self.msix), "")
        self.assertEqual(seen["argv"][:4],
                         ["powershell", "-NoProfile", "-NonInteractive",
                          "-ExecutionPolicy"])
        self.assertIn("Add-AppxPackage", seen["argv"][-1])
        self.assertIn(str(self.msix), seen["argv"][-1])

    def test_newer_version_already_installed_is_not_a_failure(self):
        err = ("Add-AppxPackage : Deployment failed with HRESULT: 0x80073D06, "
               "A newer version is already installed").encode()
        with mock.patch.object(win_backend, "run_capture",
                               return_value=(b"", err, False, 1)):
            self.assertEqual(win_backend._install_appx(self.msix), "")


class AppxPackageTests(unittest.TestCase):
    """Add-AppxPackage 的那些"其实已经在了"的返回码，不能当成失败。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.pkg = self.tmp / "x.appx"
        self.pkg.write_bytes(b"pkg")

    def _run_with(self, rc, err=b""):
        return mock.patch.object(win_backend, "run_capture",
                                 return_value=(b"", err, False, rc))

    def test_same_version_already_installed_is_success(self):
        err = (b"Add-AppxPackage : Deployment failed with HRESULT: 0x80073CFB, "
               b"The package is already installed.")
        with self._run_with(1, err):
            self.assertEqual(win_backend._add_appx_package(self.pkg), "")

    def test_resource_package_already_registered_is_success(self):
        err = b"error 0x80073D02: Unable to install because the packages are in use."
        with self._run_with(1, err):
            self.assertEqual(win_backend._add_appx_package(self.pkg), "")

    def test_failure_keeps_the_last_lines(self):
        """用户 2026-10-30 的日志只剩「返回 1: and」，什么都看不出来。"""
        err = (b"Add-AppxPackage : Deployment failed with HRESULT: 0x80073CFF\r\n"
               b"App package deployment failed with the following error\r\n"
               b"0x80073CFF: To install this application you need either a "
               b"Windows Developer license or a sideloading-enabled system.")
        with self._run_with(1, err):
            why = win_backend._add_appx_package(self.pkg)
        self.assertIn("返回 1", why)
        self.assertIn("0x80073CFF", why)
        self.assertIn("sideloading", why)

    def test_deps_are_installed_before_the_main_package(self):
        seen = []
        with mock.patch.object(win_backend, "_add_appx_package",
                               side_effect=lambda p: seen.append(p.name) or ""):
            self.assertEqual(win_backend._install_appx(
                self.pkg, [self.tmp / "dep1.appx", self.tmp / "dep2.appx"]), "")
        self.assertEqual(seen, ["dep1.appx", "dep2.appx", "x.appx"])

    def test_dep_failure_does_not_block_the_main_package(self):
        """依赖可能本来就在；主包装上了就算成功，但原因要带出去。"""
        calls = []

        def fake_add(path):
            calls.append(path.name)
            return "依赖装不上" if path.name.startswith("dep") else ""

        with mock.patch.object(win_backend, "_add_appx_package", side_effect=fake_add):
            why = win_backend._install_appx(self.pkg, [self.tmp / "dep1.appx"])
        self.assertEqual(why, "")
        self.assertIn("x.appx", calls)

    def test_both_failing_reports_both(self):
        with mock.patch.object(win_backend, "_add_appx_package",
                               side_effect=lambda p: f"{p.name} 不行"):
            why = win_backend._install_appx(self.pkg, [self.tmp / "dep1.appx"])
        self.assertIn("x.appx 不行", why)
        self.assertIn("dep1.appx 不行", why)


class WingetDepsTests(unittest.TestCase):
    """主包依赖 UI.Xaml / VCLibs 两个框架包，漏了 Add-AppxPackage 只回一个干巴巴的 1。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.payload = b"MZfake"
        import hashlib
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.deps = (("dep1.appx", len(self.payload), self.sha),)
        self.deps_dir = self.tmp / "deps"

    def _patch_env(self):
        return [
            mock.patch.object(win_backend, "WINGET_DEPS", self.deps),
            mock.patch.object(win_backend.tempfile, "gettempdir",
                              return_value=str(self.tmp)),
        ]

    def test_downloads_and_verifies(self):
        def fake_download(url, dest, on_progress=None, **kw):
            Path(dest).write_bytes(self.payload)
            return ""

        p = self._patch_env()
        with p[0], p[1], \
             mock.patch.object(win_backend, "_download",
                               side_effect=fake_download) as dl:
            files, why = win_backend._fetch_winget_deps()
        self.assertEqual(why, "")
        self.assertEqual([f.name for f in files], ["dep1.appx"])
        self.assertTrue(files[0].is_file())
        self.assertIn("dep1.appx", dl.call_args.args[0])
        self.assertEqual(dl.call_args.kwargs["sha256"], self.sha)

    def test_reuses_a_verified_copy_without_touching_the_network(self):
        target = self.tmp / "aikaui-winget-deps"
        target.mkdir()
        (target / "dep1.appx").write_bytes(self.payload)
        p = self._patch_env()
        with p[0], p[1], mock.patch.object(win_backend, "_download") as dl:
            files, why = win_backend._fetch_winget_deps()
        dl.assert_not_called()
        self.assertEqual(why, "")
        self.assertEqual(len(files), 1)

    def test_a_corrupt_copy_is_downloaded_again(self):
        target = self.tmp / "aikaui-winget-deps"
        target.mkdir()
        (target / "dep1.appx").write_bytes(b"half")       # 大小对不上

        def fake_download(url, dest, on_progress=None, **kw):
            Path(dest).write_bytes(self.payload)
            return ""

        p = self._patch_env()
        with p[0], p[1], \
             mock.patch.object(win_backend, "_download",
                               side_effect=fake_download) as dl:
            files, why = win_backend._fetch_winget_deps()
        self.assertEqual(dl.call_count, 1)
        self.assertEqual(why, "")

    def test_download_failure_is_a_reason_not_an_exception(self):
        p = self._patch_env()
        with p[0], p[1], \
             mock.patch.object(win_backend, "_download",
                               return_value="HTTP 500"):
            files, why = win_backend._fetch_winget_deps()
        self.assertEqual(files, [])
        self.assertIn("dep1.appx", why)
        self.assertIn("HTTP 500", why)


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.msix = self.tmp / "AppInstaller_x64.msix"
        with zipfile.ZipFile(self.msix, "w") as z:
            z.writestr("winget.exe", b"MZ")
            z.writestr("WindowsPackageManager.dll", b"MZ")
            z.writestr("Assets/logo.png", b"png")
            z.writestr("../../evil.exe", b"MZ")
        self.target = self.tmp / "portable"

    def test_extracts_binaries_and_skips_assets(self):
        with mock.patch.object(win_backend, "_winget_portable_dir",
                               return_value=self.target), \
             mock.patch.object(win_backend, "run_capture",
                               return_value=(b"v1.11.430", b"", False, 0)):
            exe, why = win_backend._extract_portable(self.msix)
        self.assertEqual(why, "")
        self.assertEqual(exe, str(self.target / "winget.exe"))
        self.assertTrue((self.target / "WindowsPackageManager.dll").is_file())
        self.assertFalse((self.target / "Assets").exists())
        self.assertFalse((self.tmp.parent / "evil.exe").exists())

    def test_broken_binary_is_reported(self):
        with mock.patch.object(win_backend, "_winget_portable_dir",
                               return_value=self.target), \
             mock.patch.object(win_backend, "run_capture",
                               side_effect=OSError("ERROR_BAD_EXE_FORMAT")):
            exe, why = win_backend._extract_portable(self.msix)
        self.assertIsNone(exe)
        self.assertIn("便携版跑不起来", why)

    def test_missing_winget_exe(self):
        empty = self.tmp / "empty.msix"
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("readme.txt", b"")
        with mock.patch.object(win_backend, "_winget_portable_dir",
                               return_value=self.tmp / "p2"):
            exe, why = win_backend._extract_portable(empty)
        self.assertIsNone(exe)
        self.assertIn("没有 winget.exe", why)


class InstallVscodeUsesWingetTests(unittest.TestCase):
    def test_bootstraps_winget_then_installs(self):
        info = {"exe": r"D:\vs code\Code.exe"}
        with mock.patch.object(win_backend, "find_vscode", side_effect=[None, info]), \
             mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget",
                               return_value={"ok": True, "exe": r"C:\p\winget.exe",
                                             "mode": "appx", "note": ""}) as ensure, \
             mock.patch.object(win_backend, "_install_from_setup") as setup, \
             mock.patch.object(win_backend, "run_capture",
                               return_value=(b"", b"", False, 0)) as run:
            text = win_backend.install_vscode()

            self.assertEqual(text, f"已安装 VSCode（{info['exe']}）")
            argv = run.call_args.args[0]
        ensure.assert_called_once()
        setup.assert_not_called()               # 正式装上了就不用下 240MB
        self.assertEqual(argv[0], r"C:\p\winget.exe")

    def test_portable_winget_skips_straight_to_the_installer(self):
        """便携版 winget 没注册成 AppX，源索引永远打不开 —— 别再让它试一次。

        2026-10-30 用户那次：先花时间下 58MB、解压出便携版，再用它 install，
        必然返回 0x8A15004B，最后还是得下 240MB。这里把中间那步直接砍掉。"""
        info = {"exe": r"D:\Code.exe"}
        notes = []
        with mock.patch.object(win_backend, "find_vscode",
                               side_effect=[None, info]), \
             mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget",
                               return_value={"ok": True, "exe": r"C:\p\winget.exe",
                                             "mode": "portable", "note": ""}), \
             mock.patch.object(win_backend, "run_capture") as run, \
             mock.patch.object(win_backend, "_install_from_setup",
                               return_value=(f"已安装 VSCode（{info['exe']}）", "")) as setup:
            text = win_backend.install_vscode(on_note=notes.append)

        self.assertEqual(text, f"已安装 VSCode（{info['exe']}）")
        setup.assert_called_once()
        run.assert_not_called()                 # 便携版 winget 一次都没跑
        self.assertTrue(any("便携版" in n for n in notes), notes)

    def test_bootstrap_failure_still_tries_the_official_installer(self):
        """连 winget 都没装成，不能就此放弃 —— 直接下微软的安装包。"""
        with mock.patch.object(win_backend, "find_vscode", return_value=None), \
             mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget",
                               return_value={"ok": False, "exe": "", "mode": "download",
                                             "note": "下载 winget 失败（HTTP 500）"}), \
             mock.patch.object(win_backend, "_install_from_setup",
                               return_value=(r"已安装 VSCode（D:\Code.exe）", "")):
            text = win_backend.install_vscode()
        self.assertEqual(text, r"已安装 VSCode（D:\Code.exe）")

    def test_bootstrap_failure_opens_the_download_page(self):
        with mock.patch.object(win_backend, "find_vscode", return_value=None), \
             mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget",
                               return_value={"ok": False, "exe": "", "mode": "download",
                                             "note": "下载 winget 失败（HTTP 500）"}), \
             mock.patch.object(win_backend, "_install_from_setup",
                               return_value=("", "下载 VSCode 安装包失败（离线）")), \
             mock.patch.object(common, "open_url", return_value=True):
            text = win_backend.install_vscode()
        self.assertTrue(text.startswith("没能自动装 VSCode"), text)
        self.assertIn("下载 winget 失败", text)
        self.assertIn(common.VSCODE_DOWNLOAD_URL, text)

    def test_progress_callback_is_forwarded(self):
        seen = []

        def fake_ensure(on_progress=None, on_note=None):
            on_progress(10, 100, "winget")
            return {"ok": True, "exe": r"C:\p\winget.exe", "mode": "appx", "note": ""}

        with mock.patch.object(win_backend, "find_vscode",
                               side_effect=[None, {"exe": r"D:\Code.exe"}]), \
             mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "ensure_winget", side_effect=fake_ensure), \
             mock.patch.object(win_backend, "run_capture",
                               return_value=(b"", b"", False, 0)):
            win_backend.install_vscode(on_progress=lambda d, t, what: seen.append((d, t, what)))
        self.assertEqual(seen, [(10, 100, "winget")])


class SourceIndexTests(unittest.TestCase):
    """winget 的源索引没准备好 → 0x8A15004B，用户 2026-10-30 就是撞在这上面。"""

    def test_source_problem_is_recognised(self):
        self.assertTrue(win_backend._looks_like_source_problem(
            "返回 2316632139：... SourceOpenFailedSuggestion ..."))
        self.assertTrue(win_backend._looks_like_source_problem("返回 1：source update failed"))

    def test_timeout_is_not_a_source_problem(self):
        self.assertFalse(win_backend._looks_like_source_problem("超过 900 秒没装完"))

    def test_refresh_resets_and_retries(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[1:])
            return (b"", b"", False, 0)

        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            self.assertEqual(win_backend._prepare_winget_sources(r"C:\w\winget.exe"), "")
        self.assertEqual(calls, [["source", "update"]])

    def test_failed_update_resets_then_updates_again(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv[1:])
            if argv[1:] == ["source", "update"] and len(seen) == 1:
                return (b"", b"boom", False, 1)
            return (b"", b"", False, 0)

        notes = []
        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            why = win_backend._prepare_winget_sources(r"C:\w\winget.exe", notes.append)
        self.assertEqual(why, "")
        self.assertEqual(seen, [["source", "update"],
                                ["source", "reset", "--force"],
                                ["source", "update"]])
        self.assertTrue(any("源索引" in n for n in notes))

    def test_install_retries_once_after_refreshing_sources(self):
        results = [(b"", b"SourceOpenFailedSuggestion", False, 1),
                   (b"", b"", False, 0)]
        with mock.patch.object(win_backend, "find_vscode",
                               side_effect=[None, {"exe": r"D:\Code.exe"}]), \
             mock.patch.object(win_backend, "_prepare_winget_sources",
                               return_value="") as prepare, \
             mock.patch.object(win_backend, "run_capture",
                               side_effect=lambda *a, **k: results.pop(0)):
            text, why = win_backend._install_via_winget(r"C:\w\winget.exe")
        self.assertEqual(text, r"已安装 VSCode（D:\Code.exe）")
        self.assertEqual(why, "")
        prepare.assert_called_once()

    def test_non_source_failure_does_not_retry(self):
        with mock.patch.object(win_backend, "_prepare_winget_sources") as prepare, \
             mock.patch.object(win_backend, "run_capture",
                               return_value=(b"", b"boom", False, 1)):
            text, why = win_backend._install_via_winget(r"C:\w\winget.exe")
        prepare.assert_not_called()
        self.assertEqual(text, "")
        self.assertIn("winget 返回 1", why)


class SetupInstallerTests(unittest.TestCase):
    """最后一条路：不经过 winget，下微软官方的用户级安装包静默装。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.setup = self.tmp / "VSCodeUserSetup-x64.exe"
        self.setup.write_bytes(b"MZ" + b"0" * 64)

    def test_runs_silent_flags_and_deletes_the_file(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return (b"", b"", False, 0)

        with mock.patch.object(win_backend, "_fetch_vscode_setup",
                               return_value=(self.setup, "")), \
             mock.patch.object(win_backend, "find_vscode",
                               return_value={"exe": r"D:\Code.exe"}), \
             mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            text, why = win_backend._install_from_setup()
        self.assertEqual(why, "")
        self.assertEqual(text, r"已安装 VSCode（D:\Code.exe）")
        self.assertEqual(seen["argv"][1:], ["/VERYSILENT", "/NORESTART", "/SP-",
                                            "/SUPPRESSMSGBOXES", "/MERGETASKS=!runcode"])
        self.assertFalse(self.setup.exists(), "240MB 的安装包用完要删掉")

    def test_reboot_required_is_success(self):
        with mock.patch.object(win_backend, "_fetch_vscode_setup",
                               return_value=(self.setup, "")), \
             mock.patch.object(win_backend, "find_vscode",
                               return_value={"exe": r"D:\Code.exe"}), \
             mock.patch.object(win_backend, "run_capture",
                               return_value=(b"", b"", False, 3010)):
            text, why = win_backend._install_from_setup()
        self.assertEqual(why, "")
        self.assertTrue(text.startswith("已安装 VSCode"))

    def test_download_failure_is_reported(self):
        with mock.patch.object(win_backend, "_fetch_vscode_setup",
                               return_value=(None, "HTTP 403")):
            text, why = win_backend._install_from_setup()
        self.assertEqual(text, "")
        self.assertIn("HTTP 403", why)

    def test_rejects_a_non_executable_payload(self):
        fake = _fake_requests(_FakeResp(b"<html>not found</html>" * 400000))
        with mock.patch.dict(sys.modules, {"requests": fake}):
            got, why = win_backend._fetch_vscode_setup()
        self.assertIsNone(got)
        self.assertIn("不是 Windows 可执行文件", why)


class BrokenAliasTests(unittest.TestCase):
    """WindowsApps 下的执行别名可能是空壳：文件在，进程创建直接失败。"""

    def test_verify_skips_an_unlaunchable_alias(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        alias = tmp / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe"
        alias.parent.mkdir(parents=True)
        alias.write_bytes(b"")

        with mock.patch.object(win_backend, "HOME", tmp), \
             mock.patch.object(win_backend.shutil, "which", return_value=None), \
             mock.patch.object(win_backend, "run_capture",
                               side_effect=FileNotFoundError("WinError 3")):
            self.assertIsNone(win_backend.find_winget(verify=True))
            # 不验的时候还是照旧返回（老调用方靠它只做"有没有"的判断）
            self.assertEqual(win_backend.find_winget(), str(alias))


class ShippedPackageTests(unittest.TestCase):
    """app 里硬编码的版本/大小/哈希必须自洽，别改了一半。"""

    def test_constants_are_sane(self):
        self.assertRegex(win_backend.WINGET_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(win_backend.WINGET_MSIX_SHA256, r"^[0-9a-f]{64}$")
        self.assertGreater(win_backend.WINGET_MSIX_SIZE, 50 * 1024 * 1024)
        for base in win_backend.WINGET_BASES:
            self.assertTrue(base.startswith("https://"), base)
            self.assertIn(win_backend.WINGET_VERSION, base)
            self.assertTrue(base.endswith("/"), base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
