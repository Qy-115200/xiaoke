# -*- coding: utf-8 -*-
"""没 winget 时自动把它装上 —— 全离线，一个字节都不下载。

背景（用户 2026-10-30 遇到的那次）：装 VSCode 走到一半，报「这台机器上没有 winget」，
流程就断了。所以现在没 winget 会先下微软官方的 App Installer（x64 那个 msix），
装完再用 winget 装 VSCode。

这里钉住的行为：
  · 本来就有 winget 就一次网络都不走；
  · 下的包必须过 sha256，不对就删掉重来，绝不拿去装进系统；
  · AppX 装不上就认输、把原因带回去，**不再解压便携版**（它装不了任何东西）；
  · 装箱的临时文件用完要删掉（58MB）；
  · 哪一步失败都返回一句人话，不抛异常、不留半截状态。
"""

import io
import json
import sys
import tempfile
import unittest
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
        # 最后那道"问 AppX 注册表"会真的起 PowerShell 查本机 —— 测试里关掉，
        # 否则 CI / 开发机上装着 App Installer 会让"找不到"的用例反过来红。
        patcher = mock.patch.object(win_backend, "_winget_from_appx_registration",
                                    return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_leftover_portable_copy_is_ignored(self):
        """老版本解压出来的便携版还躺在原地 —— 别再拿它：它装不了任何东西
        （没注册成 AppX 包，源索引永远打不开，`winget install` 必然 0x8A15004B），
        拿它只会让 install_vscode 白撞一次墙、白等一个超时。"""
        portable = self.tmp / "AppData" / "Local" / "aikaui" / "winget" / \
            win_backend.WINGET_VERSION / "winget.exe"
        portable.parent.mkdir(parents=True)
        portable.write_bytes(b"")
        with mock.patch.object(win_backend, "HOME", self.tmp), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertIsNone(win_backend.find_winget())

    def test_nothing_found(self):
        with mock.patch.object(win_backend, "HOME", self.tmp / "nohome"), \
             mock.patch.object(win_backend.shutil, "which", return_value=None):
            self.assertIsNone(win_backend.find_winget())

    def test_asks_appx_registration_before_giving_up(self):
        """2026-10-31 用户机器上：PATH 里最前面那个 WindowsApps 属于**另一个账户**
        （空壳别名，一创建进程就 WinError 3），HOME 也指向一个没装 App Installer
        的目录。那几个"猜"的候选全落空，但 winget 明明是好的。
        所以最后必须问一次系统自己知道答案的地方。"""
        installed = r"C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_1.29.380.0_x64__8wekyb3d8bbwe\winget.exe"
        with mock.patch.object(win_backend, "HOME", self.tmp / "nohome"), \
             mock.patch.object(win_backend.shutil, "which", return_value=None), \
             mock.patch.object(win_backend, "_winget_from_appx_registration",
                               return_value=installed):
            self.assertEqual(win_backend.find_winget(verify=True), installed)

    def test_appx_registration_candidate_is_verified_too(self):
        """找出来的那个也得真跑得起来，否则等于把 WinError 3 往后挪一步。"""
        with mock.patch.object(win_backend, "HOME", self.tmp / "nohome"), \
             mock.patch.object(win_backend.shutil, "which", return_value=None), \
             mock.patch.object(win_backend, "_winget_from_appx_registration",
                               return_value=r"C:\x\winget.exe"), \
             mock.patch.object(win_backend, "_winget_runs", return_value=False):
            self.assertIsNone(win_backend.find_winget(verify=True))


class AppxRegistrationLookupTests(unittest.TestCase):
    """`_winget_from_appx_registration` 自己：只解析，不许抛。

    ★ 单独一个类：FindWingetTests.setUp 会把整个函数替换掉（免得真起 PowerShell），
      在这里调它就成了调替身。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_reads_install_location(self):
        pkg = self.tmp / "Microsoft.DesktopAppInstaller_1.29.380.0_x64__8wekyb3d8bbwe"
        pkg.mkdir()
        (pkg / "winget.exe").write_bytes(b"")
        with mock.patch.object(win_backend, "run_capture",
                               return_value=(str(pkg).encode() + b"\r\n", b"", False, 0)):
            got = win_backend._winget_from_appx_registration()
        self.assertEqual(got, str(pkg / "winget.exe"))

    def test_location_without_winget_exe_is_ignored(self):
        pkg = self.tmp / "broken"
        pkg.mkdir()
        with mock.patch.object(win_backend, "run_capture",
                               return_value=(str(pkg).encode(), b"", False, 0)):
            self.assertIsNone(win_backend._winget_from_appx_registration())

    def test_gives_up_quietly(self):
        """没装 / PowerShell 挂了 / 超时 —— 一律 None，不许抛。"""
        for returns in ((b"", b"", False, 0),
                        (b"", b"boom", False, 1),
                        (b"", b"", True, -1)):
            with mock.patch.object(win_backend, "run_capture", return_value=returns):
                self.assertIsNone(win_backend._winget_from_appx_registration())

    def test_run_capture_exploding_is_not_fatal(self):
        with mock.patch.object(win_backend, "run_capture",
                               side_effect=OSError("nope")):
            self.assertIsNone(win_backend._winget_from_appx_registration())


class AppInstallerVersionTests(unittest.TestCase):
    """把机器上那个「应用安装程序」的版本读出来，说给用户听。"""

    def test_version_is_read(self):
        with mock.patch.object(win_backend, "run_capture",
                               return_value=(b"1.0.30251.0\r\n", b"", False, 0)):
            self.assertEqual(win_backend._app_installer_version(), "1.0.30251.0")

    def test_empty_when_not_installed(self):
        with mock.patch.object(win_backend, "run_capture",
                               return_value=(b"\r\n", b"", False, 0)):
            self.assertEqual(win_backend._app_installer_version(), "")

    def test_quiet_on_failure(self):
        with mock.patch.object(win_backend, "run_capture",
                               side_effect=OSError("nope")):
            self.assertEqual(win_backend._app_installer_version(), "")

    def test_1_0_has_no_winget_binary(self):
        """1.0.30251.0 是 winget 出现之前的老版本 —— 这次用户的机器就是它。"""
        self.assertFalse(win_backend._has_winget_binary("1.0.30251.0"))
        self.assertFalse(win_backend._has_winget_binary("1.10.9.0"))

    def test_1_11_and_newer_do(self):
        self.assertTrue(win_backend._has_winget_binary("1.11.430.0"))
        self.assertTrue(win_backend._has_winget_binary("1.29.380.0"))

    def test_garbage_does_not_cause_a_wrong_verdict(self):
        self.assertTrue(win_backend._has_winget_binary(""))


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
             mock.patch.object(win_backend, "_install_appx", return_value="") as appx:
            got = win_backend.ensure_winget()
        appx.assert_called_once()
        self.assertEqual(got["mode"], "appx")
        self.assertEqual(got["exe"], alias)
        self.assertFalse(self.msix.exists(), "58MB 的安装包用完要删掉")

    def test_appx_failure_gives_up_instead_of_extracting_portable(self):
        """便携版装不了任何东西（源索引永远打不开），所以别再解压了 ——
        以前会解压一份、打"已装好 winget（便携版）"、下一句又说"读不到软件源"，
        既白费时间又自相矛盾（2026-10-31）。现在直接把原因交回去，让
        install_vscode 去走官方安装包那条路。"""
        notes = []
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx",
                               return_value="Add-AppxPackage 返回 1：0x80073CFF"):
            got = win_backend.ensure_winget(on_note=notes.append)
        self.assertFalse(got["ok"])
        self.assertIn("0x80073CFF", got["note"])
        self.assertTrue(any("官方安装包" in n for n in notes), notes)
        # 不能出现自相矛盾的"已装好 winget"
        self.assertFalse(any("已装好" in n for n in notes), notes)

    def test_failure_note_carries_the_appx_reason(self):
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx", return_value="AppX 被禁"):
            got = win_backend.ensure_winget()
        self.assertFalse(got["ok"])
        self.assertIn("AppX 被禁", got["note"])

    def test_ancient_app_installer_is_named_in_the_log(self):
        """用户机器上注册的是 1.0.30251.0 —— winget 出现之前的老版本。

        只说"装不上"会让人以为是环境坏了；把版本说出来，用户才知道
        去 Store 里更新一下它就能一劳永逸（2026-10-31）。"""
        notes = []
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx",
                               return_value="Add-AppxPackage 返回 1：0x80073CF9"), \
             mock.patch.object(win_backend, "_app_installer_version",
                               return_value="1.0.30251.0"):
            got = win_backend.ensure_winget(on_note=notes.append)
        self.assertFalse(got["ok"])
        self.assertTrue(any("1.0.30251.0" in n and "Store" in n for n in notes), notes)

    def test_modern_app_installer_is_not_lectured_about(self):
        """版本是新的就别说"太老"那句 —— 那时候问题是别的（部署报错）。"""
        notes = []
        with mock.patch.object(win_backend, "find_winget", return_value=None), \
             mock.patch.object(win_backend, "_fetch_winget_msix",
                               return_value=(self.msix, "")), \
             mock.patch.object(win_backend, "_install_appx",
                               return_value="Add-AppxPackage 返回 1：0x80073CF9"), \
             mock.patch.object(win_backend, "_app_installer_version",
                               return_value="1.29.380.0"):
            got = win_backend.ensure_winget(on_note=notes.append)
        self.assertFalse(got["ok"])
        self.assertFalse(any("太老" in n for n in notes), notes)

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

    def test_install_appx_uses_path_never_literalpath(self):
        """★ 参数名只能是 `-Path`。Add-AppxPackage 根本没有 `-LiteralPath`，
        写上它 PowerShell 当场抛 ParameterBindingException /
        NamedParameterNotFound，一句都不执行 —— 表现就是"返回 1，什么都没装"。
        2026-10-30 到 10-31 用户机器上一直是这个原因。"""
        seen = {}

        def fake_run(argv, **kw):
            seen["cmd"] = argv[-1]
            return b"", b"", False, 0

        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            win_backend._install_appx(self.msix)
        self.assertIn("-Path ", seen["cmd"])
        self.assertNotIn("LiteralPath", seen["cmd"])

    def test_wildcard_chars_go_through_set_location(self):
        """路径里有 `[` `]` 时 -Path 会当成通配模式：先 Set-Location 进目录，
        再只传文件名。文件名是我们自己起的，可控。"""
        weird = self.tmp / "[x]"
        weird.mkdir(parents=True, exist_ok=True)
        pkg = weird / "y.appx"
        pkg.write_bytes(b"pkg")

        seen = {}

        def fake_run(argv, **kw):
            seen["cmd"] = argv[-1]
            return b"", b"", False, 0

        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            win_backend._install_appx(pkg)
        self.assertIn("Set-Location -LiteralPath", seen["cmd"])
        self.assertIn("-Path 'y.appx'", seen["cmd"])

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
        # 失败要睡 2 秒等杀软松口；测试里别真等
        patch = mock.patch.object(win_backend.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

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

    def test_failure_keeps_the_hrtsult_line(self):
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

    def test_failure_summary_drops_the_position_marker(self):
        """2026-10-31 用户截图里的那种形状：正文被 `+ ~~~~` 和
        `+ Add-AppxPackage -Path …` 的回声占满，HRESULT 那行反被挤掉了。
        现在第一行（带 HRESULT）必须保住，装饰行必须扔掉。"""
        err = (b"Add-AppxPackage : Deployment failed with HRESULT: 0x80073CF6, "
               b"Package could not be registered.\r\n"
               b"At line:1 char:1\r\n"
               b"+ Add-AppxPackage -Path 'C:\\Users\\x\\a.msix' -ErrorAction Stop\r\n"
               b"+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\r\n"
               b"    + CategoryInfo          : WriteError: (C:\\Users\\x\\a.msix:String) "
               b"[Add-AppxPackage], IOException\r\n"
               b"    + FullyQualifiedErrorId : DeploymentError,Microsoft.Windows.Appx."
               b"PackageManager.Commands.AddAppxPackageCommand\r\n")
        with self._run_with(1, err):
            why = win_backend._add_appx_package(self.pkg)
        self.assertIn("0x80073CF6", why)
        self.assertIn("IOException", why)
        self.assertIn("DeploymentError", why)
        self.assertNotIn("~~~", why)
        self.assertNotIn("-ErrorAction Stop", why, "源命令回声不该占字符预算")
        self.assertLessEqual(len(why), 360, why)

    def test_chinese_path_in_the_error_is_decoded(self):
        """中文 Windows 上 PowerShell 按 GBK 写输出。按 UTF-8 硬解会把路径里的
        中文变成 `????`，用户截图就是这样 —— 连发生在哪个文件上都看不出来。"""
        err = ("Add-AppxPackage : 部署失败：找不到 C:\\Users\\张三\\a.msix"
               .encode("gbk"))
        with self._run_with(1, err):
            why = win_backend._add_appx_package(self.pkg)
        self.assertIn("张三", why)
        self.assertNotIn("????", why)

    def test_failure_is_retried_once_after_a_pause(self):
        """杀软可能正扫着刚下来的包 —— 隔一会儿再试一次常常就过了。"""
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[-1])
            rc = 1 if len(calls) == 1 else 0
            return b"", (b"Deployment failed with HRESULT: 0x80073D05" if rc else b""), False, rc

        with mock.patch.object(win_backend, "run_capture", side_effect=fake_run):
            why = win_backend._install_appx(self.pkg)
        self.assertEqual(why, "")
        self.assertEqual(len(calls), 2)
        win_backend.time.sleep.assert_called()      # 试之前先歇了一下

    def test_two_failures_report_both_attempts(self):
        calls = []

        def fake_add(path, **kw):
            calls.append(path.name)
            return "装不上"

        with mock.patch.object(win_backend, "_add_appx_package", side_effect=fake_add):
            why = win_backend._install_appx(self.pkg)
        self.assertIn("装不上", why)
        self.assertIn("再试一次", why)
        self.assertEqual(calls, ["x.appx", "x.appx"], "主包两次")

    def test_ascii_copy_only_when_the_path_has_non_ascii(self):
        """包路径本来就全是 ASCII 时不要白复制 58MB。"""
        self.assertIsNone(win_backend._ascii_copy(self.pkg))

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


class _RangeResp:
    """带 Range 的假响应：只吐 [start, end] 这一段，并回 206 + content-range。"""

    def __init__(self, payload: bytes, start: int, end: int, total=None):
        total = len(payload) if total is None else total
        self._chunk = payload[start:end + 1]
        self.status_code = 206
        self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
        self.url = "https://cdn.example/VSCodeUserSetup-x64.exe"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self._chunk), chunk_size):
            yield self._chunk[i:i + chunk_size]


class _RangeRequests:
    """假的 requests：认 Range 头。不带 Range 就当普通 200 全量（单连接那条路）。"""

    def __init__(self, payload: bytes, *, honor_range=True, fail_at=None, api=None):
        self.payload = payload
        self.honor_range = honor_range
        self.fail_at = fail_at
        self.api = api
        self.seen_ranges = []

    def get(self, url, headers=None, stream=False, timeout=None, **kw):
        if url == win_backend.VSCODE_UPDATE_API:
            return _JsonResp(self.api if self.api is not None else {})
        rng = (headers or {}).get("Range")
        if not rng:
            return _FakeResp(self.payload)
        spec = rng.split("=", 1)[1]
        s, e = (int(x) for x in spec.split("-"))
        self.seen_ranges.append((s, e))
        if not self.honor_range:
            return _FakeResp(self.payload)              # 老服务器：无视 Range 回 200
        if self.fail_at is not None and s == self.fail_at:
            return _FakeResp(b"", status=500)
        return _RangeResp(self.payload, s, e)


class _JsonResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data


class ParallelDownloadTests(unittest.TestCase):
    """240MB 那个安装包：6 路分段下（2026-10-31 加）。

    起因是用户的日志：「直接下载微软官方的安装包花的时间太久了」。本机实测
    单连接 ~450KB/s（240MB 要 9 分钟），6 路并发 ~2.8MB/s（一分半）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.payload = bytes(range(256)) * 8192          # 2MB，内容可验证
        self.dest = self.tmp / "vscode.exe"
        for name, val in (("_PARALLEL_MIN_SIZE", 1024),
                          ("_PARALLEL_PIECE_FLOOR", 4096),
                          ("_PARALLEL_REPORT_EVERY", 0.0)):
            p = mock.patch.object(win_backend, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(mock.patch.object(win_backend, "is_cancelled",
                                          return_value=False).stop)

    def _run(self, fake):
        with mock.patch.dict(sys.modules, {"requests": fake}):
            return win_backend._download_parallel(
                "https://update.code.visualstudio.com/latest/win32-x64-user/stable",
                self.dest, pieces=4)

    def test_pieces_are_reassembled_in_order(self):
        why = self._run(_RangeRequests(self.payload))
        self.assertEqual(why, "")
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual(list(self.tmp.glob("*.part*")), [], "分片要清干净")

    def test_sha256_is_checked(self):
        fake = _RangeRequests(self.payload)
        with mock.patch.dict(sys.modules, {"requests": fake}):
            why = win_backend._download_parallel(
                "https://x/latest", self.dest, pieces=4, sha256="0" * 64)
        self.assertIn("sha256", why)
        self.assertFalse(self.dest.exists())

    def test_server_without_range_support_gives_up(self):
        why = self._run(_RangeRequests(self.payload, honor_range=False))
        self.assertIn("不支持分段", why)
        self.assertFalse(self.dest.exists())

    def test_one_broken_piece_leaves_nothing_behind(self):
        why = self._run(_RangeRequests(self.payload, fail_at=0))
        self.assertIn("500", why)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.tmp.glob("*.part*")), [])

    def test_cancel_stops_and_cleans_up(self):
        with mock.patch.object(win_backend, "is_cancelled", return_value=True):
            why = self._run(_RangeRequests(self.payload))
        self.assertEqual(why, "已中断")
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.tmp.glob("*.part*")), [])

    def test_progress_is_reported(self):
        seen = []
        fake = _RangeRequests(self.payload)
        with mock.patch.dict(sys.modules, {"requests": fake}):
            win_backend._download_parallel(
                "https://x/latest", self.dest, lambda d, t, k: seen.append((d, t, k)),
                pieces=4)
        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], len(self.payload))
        self.assertEqual(seen[-1][2], "vscode")

    def test_tiny_file_is_not_worth_splitting(self):
        with mock.patch.object(win_backend, "_PARALLEL_MIN_SIZE", 10 << 20):
            why = self._run(_RangeRequests(self.payload))
        self.assertIn("不值得分段", why)


class VscodeSetupTests(unittest.TestCase):
    """`_fetch_vscode_setup` 的路由：先 6 路分段，不成退回单连接。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        p = mock.patch.object(win_backend.tempfile, "gettempdir",
                              return_value=str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(mock.patch.object(win_backend, "is_cancelled",
                                          return_value=False).stop)

    def test_uses_the_api_url_and_sha(self):
        """API 给了带版本号的真实地址 + sha256，就按它下 —— 下完能验哈希。"""
        seen = {}

        def fake_parallel(url, dest, on_progress=None, **kw):
            seen["url"] = url
            seen["sha"] = kw.get("sha256")
            Path(dest).write_bytes(b"MZ" + b"\0" * 64)
            return ""

        with mock.patch.object(win_backend, "_vscode_setup_target",
                               return_value=("https://cdn/x.exe", "ab" * 32, "")), \
             mock.patch.object(win_backend, "_download_parallel", fake_parallel):
            got, why = win_backend._fetch_vscode_setup()
        self.assertEqual(why, "")
        self.assertEqual(got.name, "aikaui-VSCodeUserSetup-x64.exe")
        self.assertEqual(seen["url"], "https://cdn/x.exe")
        self.assertEqual(seen["sha"], "ab" * 32)

    def test_falls_back_to_one_connection(self):
        payload = b"MZ" + b"\0" * (51 * 1024 * 1024)
        sha = __import__("hashlib").sha256(payload).hexdigest()
        fake = _RangeRequests(payload, api={"url": "https://cdn/x.exe",
                                            "sha256hash": sha})
        with mock.patch.dict(sys.modules, {"requests": fake}), \
             mock.patch.object(win_backend, "_download_parallel",
                               return_value="服务器不支持分段（HTTP 200）"):
            got, why = win_backend._fetch_vscode_setup()
        self.assertEqual(why, "")
        self.assertEqual(got.read_bytes()[:2], b"MZ")

    def test_single_connection_also_checks_the_hash(self):
        """退回单连接也不能丢掉校验：拼错一段的文件头照样是 MZ。"""
        payload = b"MZ" + b"\0" * (51 * 1024 * 1024)
        fake = _RangeRequests(payload, api={"url": "https://cdn/x.exe",
                                            "sha256hash": "cd" * 32})
        with mock.patch.dict(sys.modules, {"requests": fake}), \
             mock.patch.object(win_backend, "_download_parallel",
                               return_value="分段失败"):
            got, why = win_backend._fetch_vscode_setup()
        self.assertIsNone(got)
        self.assertIn("sha256", why)
        self.assertEqual(list(self.tmp.glob("*.part*")), [], "半截文件要清掉")

    def test_cancel_during_parallel_does_not_retry(self):
        with mock.patch.object(win_backend, "_vscode_setup_target",
                               return_value=("https://cdn/x.exe", "", "")), \
             mock.patch.object(win_backend, "_download_parallel",
                               return_value="已中断"), \
             mock.patch.object(win_backend, "_fetch_vscode_setup_single") as single:
            got, why = win_backend._fetch_vscode_setup()
        single.assert_not_called()          # 用户说停就停，别再偷偷下一遍
        self.assertIsNone(got)
        self.assertEqual(why, "已中断")

    def test_no_api_still_downloads(self):
        payload = b"MZ" + b"\0" * (51 * 1024 * 1024)
        fake = _RangeRequests(payload)
        with mock.patch.dict(sys.modules, {"requests": fake}), \
             mock.patch.object(win_backend, "_vscode_setup_target",
                               return_value=("", "", "HTTP 503")):
            got, why = win_backend._fetch_vscode_setup()
        self.assertEqual(why, "")
        self.assertTrue(got.is_file())


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
