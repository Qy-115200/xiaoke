# -*- coding: utf-8 -*-
"""packaging/install.sh：真跑一遍 shell（假 curl + 真 tar），不碰外网。

这个脚本是用户 `curl | sh` 直接执行的东西，改错一行就是全体安装失败，
所以这里测的是**行为**：装到哪、校验不过时退不退出、包里带 `../` 会不会接、
notes 里混进 `"linux-x64": {` 这样的文字会不会把解析搞乱。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "install.sh"
SH = shutil.which("sh")

GOOD_VERSION = "1.2.0"
PKG_NAME = "ClaudeCodeAssistant-linux-x64.tar.gz"
FAKE_APP = b'#!/bin/sh\necho "ClaudeCodeAssistant 1.2.0"\n'

FAKE_CURL = r'''#!/bin/sh
# 假 curl：把 URL 的最后一段当成 $FAKE_ROOT 下的文件名
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --retry|--connect-timeout|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
name="${url##*/}"
src="$FAKE_ROOT/$name"
[ -f "$src" ] || { echo "fake curl: 404 $url" >&2; exit 22; }
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
'''

FAKE_ID = r'''#!/bin/sh
echo "${FAKE_UID:-1000}"
'''


def _tarball(members) -> bytes:
    """members: [(路径, 内容 bytes, mode)] → .tar.gz 字节。"""
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            tf.addfile(info, BytesIO(body))
    return buf.getvalue()


def _manifest(pkg_sha, pkg_size, notes="", url=None):
    """和 build_tools/make_manifest.py 完全一样的形状（indent=2）。"""
    base = f"https://dl.example.invalid/releases/{GOOD_VERSION}"
    linux = {
        "version": GOOD_VERSION,
        "url": url or f"{base}/{PKG_NAME}",
        "sha256": pkg_sha,
        "size": pkg_size,
        "min_version": "1.0.0",
        "notes": notes,
    }
    data = {
        "schema_version": 1,
        "latest": {
            "windows-x64": dict(linux, url=f"{base}/ClaudeCodeAssistant-win-x64.exe"),
            "macos-arm64": dict(linux, url=f"{base}/ClaudeCodeAssistant-macos-arm64.zip"),
            "macos-x64": dict(linux, url=f"{base}/ClaudeCodeAssistant-macos-x64.zip"),
            "linux-x64": linux,
        },
    }
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@unittest.skipUnless(SH, "这台机器上没有 sh")
class InstallShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_root = self.root / "http"
        self.fake_root.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._write(self.bin / "curl", FAKE_CURL)
        self._write(self.bin / "id", FAKE_ID)

    # ---------- 工具 ----------
    def _write(self, path, text, mode=0o755):
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def _env(self, **over):
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "FAKE_ROOT": str(self.fake_root),
            "FAKE_UID": "1000",
            "AIKAUI_SITE": "https://example.invalid",
            "AIKAUI_NO_RUN": "1",
            "TMPDIR": str(self.root / "tmp"),
            "SHELL": "/bin/bash",
        })
        env.pop("AIKAUI_ALLOW_ROOT", None)
        (self.root / "tmp").mkdir(exist_ok=True)
        env.update(over)
        return env

    def _serve(self, tarball, sha=None, notes="", url=None, size=None):
        (self.fake_root / PKG_NAME).write_bytes(tarball)
        manifest = _manifest(
            sha if sha is not None else hashlib.sha256(tarball).hexdigest(),
            size if size is not None else len(tarball),
            notes=notes, url=url)
        (self.fake_root / "latest.json").write_bytes(manifest)

    def _run(self, **over):
        return subprocess.run([SH, str(SCRIPT)], env=self._env(**over),
                              capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace")

    def _good_pkg(self):
        return _tarball([
            ("ClaudeCodeAssistant-linux-x64/ClaudeCodeAssistant", FAKE_APP, 0o755),
            ("ClaudeCodeAssistant-linux-x64/agent.png", b"png", 0o644),
        ])

    # ---------- 正常路径 ----------
    def test_happy_path_installs_and_writes_path(self):
        self._serve(self._good_pkg())
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(GOOD_VERSION, r.stdout)

        exe = self.home / ".local" / "lib" / "claude-code-assistant" / "ClaudeCodeAssistant"
        self.assertTrue(exe.is_file())
        self.assertTrue(os.access(exe, os.R_OK))
        self.assertTrue((self.home / ".local" / "bin" / "claude-code-assistant").exists())

        bashrc = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual(bashrc.count("# >>> aikaui >>>"), 1)
        self.assertIn(".local/bin", bashrc)

    def test_rerun_is_idempotent(self):
        self._serve(self._good_pkg())
        self.assertEqual(self._run().returncode, 0)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        bashrc = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual(bashrc.count("# >>> aikaui >>>"), 1)

    def test_notes_cannot_confuse_the_parser(self):
        # notes 里原样带一段像 JSON 的文字 —— 解析器只能认平台块的开头
        tricky = '发版说明："linux-x64": {\n  "version": "9.9.9"\n}'
        self._serve(self._good_pkg(), notes=tricky)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("版本 " + GOOD_VERSION, r.stdout)
        self.assertNotIn("9.9.9", r.stdout)

    def test_crlf_manifest_is_understood(self):
        """清单可能是在 Windows 上生成的（CRLF）—— 按行匹配的 awk 不能因此认不出平台块。

        2026-10-26 真踩到了：make_manifest.py 在 Windows 上写出 CRLF，线上脚本
        报「清单里没有 linux-x64 的 version」。修法是两边都改：写清单固定 LF，
        读清单时再容忍 \\r（线上已经发出去的那份还是 CRLF）。
        """
        pkg = self._good_pkg()
        (self.fake_root / PKG_NAME).write_bytes(pkg)
        manifest = _manifest(hashlib.sha256(pkg).hexdigest(), len(pkg))
        (self.fake_root / "latest.json").write_bytes(manifest.replace(b"\n", b"\r\n"))
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("版本 " + GOOD_VERSION, r.stdout)

    def test_user_rc_content_is_preserved(self):
        (self.home / ".bashrc").write_text("# 我自己的配置\nalias ll='ls -l'\n",
                                           encoding="utf-8")
        self._serve(self._good_pkg())
        self.assertEqual(self._run().returncode, 0)
        text = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我自己的配置"))

    # ---------- 校验失败 ----------
    def test_sha_mismatch_aborts(self):
        self._serve(self._good_pkg(), sha="0" * 64)
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("校验失败", r.stderr)
        self.assertFalse((self.home / ".local" / "lib" /
                          "claude-code-assistant" / "ClaudeCodeAssistant").exists())

    def test_size_mismatch_aborts(self):
        self._serve(self._good_pkg(), size=1)
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("大小不对", r.stderr)

    def test_non_https_url_is_refused(self):
        pkg = self._good_pkg()
        self._serve(pkg, url=f"http://example.invalid/{PKG_NAME}")
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("https", r.stderr)

    def test_path_traversal_in_tarball_is_refused(self):
        evil = _tarball([
            ("ClaudeCodeAssistant-linux-x64/ClaudeCodeAssistant", FAKE_APP, 0o755),
            ("../../.ssh/authorized_keys", b"ssh-rsa AAAA\n", 0o600),
        ])
        self._serve(evil)
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("不安全的路径", r.stderr)
        self.assertFalse((self.home / ".ssh" / "authorized_keys").exists())

    def test_missing_binary_in_tarball(self):
        self._serve(_tarball([("ClaudeCodeAssistant-linux-x64/README", b"hi", 0o644)]))
        r = self._run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("找不到", r.stderr)

    def test_binary_that_cannot_run_exits_4(self):
        broken = _tarball([
            ("ClaudeCodeAssistant-linux-x64/ClaudeCodeAssistant",
             b"#!/bin/sh\nexit 1\n", 0o755),
        ])
        self._serve(broken)
        r = self._run()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("跑不起来", r.stderr)

    def test_manifest_unreachable(self):
        self._serve(self._good_pkg())
        (self.fake_root / "latest.json").unlink()
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("拉不到", r.stderr)

    # ---------- root ----------
    def test_root_is_refused_by_default(self):
        self._serve(self._good_pkg())
        r = self._run(FAKE_UID="0")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("root", r.stderr)
        self.assertFalse((self.home / ".local" / "lib").exists())

    def test_root_refusal_prints_a_copy_pasteable_command(self):
        """只写「请加 AIKAUI_ALLOW_ROOT=1」不够：curl|sh 里变量加在 curl 前面
        根本传不到 sh，用户照着想当然改一遍，回来还是一样的错。
        也别把退出码当正文印出来（die 的 $2 曾经混进消息里，用户看到 …=1 2）。"""
        self._serve(self._good_pkg())
        r = self._run(FAKE_UID="0")
        self.assertIn("| AIKAUI_ALLOW_ROOT=1 sh", r.stderr)
        self.assertNotIn("AIKAUI_ALLOW_ROOT=1 sh 2", r.stderr)
        self.assertNotIn("=1 2\n", r.stderr)

    def test_allow_root_env_lets_it_through(self):
        self._serve(self._good_pkg())
        r = self._run(FAKE_UID="0", AIKAUI_ALLOW_ROOT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_allow_root_env_tells_the_wizard_too(self):
        """★ 环境变量只在这段 shell 里有效；向导是另一个进程，会再拦一次 root。

        2026-10-27 的两个坑都在这条上：装了 tar 包、PATH 也配了，最后卡在 [1/5]
        说"请用你自己的账号运行"。所以那句「手动继续」必须带上 --allow-root。"""
        self._serve(self._good_pkg())
        r = self._run(FAKE_UID="0", AIKAUI_ALLOW_ROOT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--allow-root", r.stdout)
        self.assertIn("root", r.stderr)          # 也提醒一句：东西都落在 /root 下

    def test_normal_user_is_not_given_allow_root(self):
        self._serve(self._good_pkg())
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("--allow-root", r.stdout)


class ScriptShapeTests(unittest.TestCase):
    """不跑，只检查脚本本身的几条硬约束。"""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")
        # 注释里出现 sudo / 1.2.0 / <<< 都不算违规（那是在解释规则）
        self.code = "\n".join(
            line for line in self.text.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_posix_shebang_and_strict_mode(self):
        self.assertTrue(self.text.startswith("#!/bin/sh"))
        self.assertIn("set -eu", self.text)

    def test_no_bash_only_syntax(self):
        for bad in ("[[", "local ", "function ", "pipefail", "$'"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, self.code)

    def test_no_hardcoded_version(self):
        import re
        # 版本必须来自 latest.json；写死一个 X.Y.Z 就说明有人图省事
        self.assertIsNone(re.search(r"\b\d+\.\d+\.\d+\b", self.code))

    def test_never_uses_sudo(self):
        self.assertNotIn("sudo", self.code)

    def test_cleanup_trap_exists(self):
        self.assertIn("trap cleanup", self.text)

    def test_no_crlf(self):
        """``#!/bin/sh\\r`` 在 Linux 上是"找不到解释器"；.gitattributes 钉 LF，这里再兜一层。

        必须读字节：``read_text`` 会把 ``\\r\\n`` 一并翻译掉，看不出问题。
        """
        raw = SCRIPT.read_bytes()
        self.assertNotIn(b"\r", raw, "install.sh 里混进了 CRLF —— 用户 curl | sh 会直接失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
