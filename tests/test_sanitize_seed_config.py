# @feature: FP-0.2.移动端适配 移动构建产物清单 | @ci: python-coverage
"""G-M0 车道：种子配置脱敏面契约（坑㊳配套——真 key 永不进 APK）。

被测对象 deploy/sanitize_seed_config.py，两面行为契约：
- 脱敏面：明文 key 清空；${VAR} 占位符/掩码/示例值/空值保留；非 secret 字段
  与非字符串值（如 token_quota: 0）不动；yaml 解析失败 fail-closed。
- 断言面：打包 tar 内 config 条目复跑同一判定——明文 key 逐条报告（条目+路径），
  干净产物零违约；非 config 条目不扫（范围=config 内 key 字段）。
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_helper_path = _REPO_ROOT / "deploy" / "sanitize_seed_config.py"
_spec = importlib.util.spec_from_file_location("sanitize_seed_config", _helper_path)
assert _spec is not None
assert _spec.loader is not None
ssc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssc)

pytestmark = pytest.mark.unit


class TestSafeSecretValue:
    """安全值形态词表：占位符/掩码/示例值/空 = 安全；其余 = 明文。"""

    @pytest.mark.parametrize(
        "value",
        [
            "${MINIMAX_API_KEY}",  # 整串占位符（种子现役形态，必须保留）
            "",  # 空值
            "   ",  # 空白
            "****",  # 内核 GET 掩码视图形态
            "***",
            "your-key-here",  # .env.example 示例值前缀
        ],
    )
    def test_safe_forms(self, value: str) -> None:
        assert ssc.is_safe_secret_value(value)

    @pytest.mark.parametrize(
        "value",
        [
            "sk-real-abc123",  # 常见明文形态（连字符）
            "ey:xxx.yyy.zzz",  # JWT 形态
            "${INCOMPLETE",  # 仿占位符残缺形态——不是合法占位符，按明文拦截
            "$MINIMAX_API_KEY",  # 缺花括号引用——占位符解析侧不认，按明文拦截
            "sk_live_" + "a" * 24,  # 32 位 Stripe 形态——超标识符长度上限，按明文拦截
        ],
    )
    def test_unsafe_forms(self, value: str) -> None:
        assert not ssc.is_safe_secret_value(value)

    @pytest.mark.parametrize(
        "value",
        [
            "AGENTOS_BRIDGE_TOKEN",  # bridge auth.token_env 现役值（env 变量名引用）
            "godot_api_key",  # godot auth.secret_key 现役值（字段名引用）
            "ab9",  # 词形边界内最短形态（3 位）
        ],
    )
    def test_identifier_reference_forms_safe(self, value: str) -> None:
        assert ssc.is_safe_secret_value(value)


class TestViolationsInData:
    def test_placeholder_and_mask_kept_plaintext_flagged(self) -> None:
        data = {
            "providers": {
                "minimax": {"keys": [{"id": "m", "api_key": "${MINIMAX_API_KEY}"}]},
                "evil": {"keys": [{"id": "e", "api_key": "sk-real-abc"}]},
                "masked": {"api_key": "****"},
                "empty": {"api_key": ""},
            },
            "defaults": {"chat": "minimax-m3"},
        }
        hits = ssc.violations_in_data(data)
        assert hits == ["providers.evil.keys[0].api_key"]

    def test_non_string_and_partial_word_keys_untouched(self) -> None:
        # token_quota 含 "token" 但值是 int；键名包含匹配与 kernel is_secret_key 同律
        data = {"token_quota": 0, "tokens_used": 12, "api_key": "${X}", "note": "password in prose"}
        assert ssc.violations_in_data(data) == []

    def test_quoted_placeholder_is_safe_after_yaml_parse(self) -> None:
        # asr.yaml 现役形态：api_key: "${ZHIPU_API_KEY}"——yaml 载入后即占位符
        data = yaml.safe_load('asr:\n  api_key: "${ZHIPU_API_KEY}"\n')
        assert ssc.violations_in_data(data) == []


class TestSanitizeConfigDir:
    def test_plaintext_cleared_placeholder_kept(self, tmp_path: Path) -> None:
        cfg = tmp_path / "llm.yaml"
        cfg.write_text(
            "providers:\n"
            "  a:\n"
            "    keys:\n"
            "    - id: main\n"
            "      api_key: ${A_API_KEY}\n"
            "  b:\n"
            "    keys:\n"
            "    - id: main\n"
            "      api_key: sk-plaintext\n",
            encoding="utf-8",
        )
        cleared = ssc.sanitize_config_dir(tmp_path)
        assert cleared == ["llm.yaml#providers.b.keys[0].api_key"]
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["providers"]["a"]["keys"][0]["api_key"] == "${A_API_KEY}"
        assert data["providers"]["b"]["keys"][0]["api_key"] == ""

    def test_clean_dir_returns_empty_and_writes_nothing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "x.yaml"
        original = "api_key: ${X_API_KEY}\nother: keep\n"
        cfg.write_text(original, encoding="utf-8")
        assert ssc.sanitize_config_dir(tmp_path) == []
        assert cfg.read_text(encoding="utf-8") == original  # 未动一字节

    def test_unparseable_yaml_fail_closed(self, tmp_path: Path) -> None:
        (tmp_path / "bad.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        with pytest.raises(ValueError, match="fail-closed"):
            ssc.sanitize_config_dir(tmp_path)

    def test_non_yaml_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("api_key: sk-plaintext\n", encoding="utf-8")
        assert ssc.sanitize_config_dir(tmp_path) == []


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class TestSeedAssertion:
    def test_clean_seed_zero_violations(self, tmp_path: Path) -> None:
        llm = yaml.safe_dump(
            {
                "providers": {"minimax": {"keys": [{"api_key": "${MINIMAX_API_KEY}"}]}},
                "models": {"m": {"api_key": ""}},
            },
            allow_unicode=True,
        )
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes(
                {
                    "opt/agentos/config/plugins/llm/llm.yaml": llm.encode(),
                    "opt/agentos/agentos-kernel": b"ELF...",
                }
            )
        )
        assert ssc.seed_config_violations(seed) == []

    def test_plaintext_key_reported_with_entry_and_path(self, tmp_path: Path) -> None:
        dirty = yaml.safe_dump({"providers": {"a": {"api_key": "sk-real-xyz"}}})
        seed = tmp_path / "seed.tar"
        seed.write_bytes(_tar_bytes({"opt/agentos/config/plugins/llm/llm.yaml": dirty.encode()}))
        assert ssc.seed_config_violations(seed) == ["opt/agentos/config/plugins/llm/llm.yaml#providers.a.api_key"]

    def test_non_config_entries_not_scanned(self, tmp_path: Path) -> None:
        # 范围契约：断言只盯 config 面；插件代码/venv 内的字符串字面量不误报
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes(
                {
                    "opt/agentos/plugins/shared/llm/server.py": b'api_key = "sk-not-a-config"\n',
                    "etc/os-release": b"ID=ubuntu\n",
                }
            )
        )
        assert ssc.seed_config_violations(seed) == []

    def test_unparseable_entry_reported_not_swallowed(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(_tar_bytes({"opt/agentos/config/broken.yaml": b"a: [unclosed\n"}))
        hits = ssc.seed_config_violations(seed)
        assert len(hits) == 1
        assert hits[0].startswith("opt/agentos/config/broken.yaml#<yaml-parse-failed")

    def test_zstd_seed_roundtrip(self, tmp_path: Path) -> None:
        zstandard = pytest.importorskip("zstandard")
        dirty = yaml.safe_dump({"api_key": "sk-zst-leak"})
        inner = _tar_bytes({"opt/agentos/config/plugins/llm/llm.yaml": dirty.encode()})
        seed = tmp_path / "seed.tar.zst"
        with seed.open("wb") as fh:
            zstandard.ZstdCompressor().copy_stream(io.BytesIO(inner), fh)
        assert ssc.seed_config_violations(seed) == ["opt/agentos/config/plugins/llm/llm.yaml#api_key"]


class TestCli:
    def test_assert_seed_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        dirty = yaml.safe_dump({"api_key": "sk-cli-leak"})
        clean = yaml.safe_dump({"api_key": "${CLEAN_API_KEY}"})
        bad = tmp_path / "bad.tar"
        bad.write_bytes(_tar_bytes({"opt/agentos/config/a.yaml": dirty.encode()}))
        good = tmp_path / "good.tar"
        good.write_bytes(_tar_bytes({"opt/agentos/config/a.yaml": clean.encode()}))
        assert ssc.main(["assert-seed", "--seed", str(bad)]) == 1
        assert "sk-cli-leak" not in capsys.readouterr().out  # 明文值不落 stdout（零口令外泄面）
        assert ssc.main(["assert-seed", "--seed", str(good)]) == 0


class TestFactoryProfileDrift:
    """出厂画像对账（2026-11-18 个人信息出仓处置）：种子内 config/users/**/profile.md
    必须与仓内出厂模板逐字节一致——种子面出现用户真值画像即违约。"""

    def _repo_users(self, tmp_path: Path, content: str) -> Path:
        repo_users = tmp_path / "repo_users"
        (repo_users / "default").mkdir(parents=True, exist_ok=True)
        (repo_users / "default" / "profile.md").write_bytes(content.encode())  # write_bytes：对账是字节面，避免 Windows 换行翻译
        return repo_users

    def test_drift_reported(self, tmp_path: Path) -> None:
        repo_users = self._repo_users(tmp_path, "# 出厂占位\n")
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/config/users/default/profile.md": "# 真值画像\n".encode()})
        )
        hits = ssc.seed_config_violations(seed, repo_users_dir=repo_users)
        assert hits == ["opt/agentos/config/users/default/profile.md#<profile-factory-drift>"]

    def test_equal_factory_passes(self, tmp_path: Path) -> None:
        repo_users = self._repo_users(tmp_path, "# 出厂占位\n")
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/config/users/default/profile.md": "# 出厂占位\n".encode()})
        )
        assert ssc.seed_config_violations(seed, repo_users_dir=repo_users) == []

    def test_missing_repo_counterpart_reported(self, tmp_path: Path) -> None:
        repo_users = tmp_path / "repo_users"  # 空目录：种子多出的画像无出厂对应
        repo_users.mkdir()
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/config/users/default/profile.md": b"# x\n"})
        )
        hits = ssc.seed_config_violations(seed, repo_users_dir=repo_users)
        assert hits == ["opt/agentos/config/users/default/profile.md#<profile-factory-drift>"]

    def test_no_profile_entries_passes(self, tmp_path: Path) -> None:
        # 缺席不判（种子未含 users 面属打包完整性问题，由 build 自身完整性检查承载）
        repo_users = self._repo_users(tmp_path, "# 出厂占位\n")
        seed = tmp_path / "seed.tar"
        seed.write_bytes(_tar_bytes({"opt/agentos/config/a.yaml": b"k: v\n"}))
        assert ssc.seed_config_violations(seed, repo_users_dir=repo_users) == []


class TestForbiddenSeedPaths:
    """部署真值面禁入：fmin local mcp 真值配置与审计日志不得出现在种子里。"""

    def test_real_value_config_forbidden(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes(
                {
                    "opt/agentos/deploy/remote/local_mcp_server/config.json": b"{}",
                    "opt/agentos/deploy/remote/local_mcp_server/logs/audit.jsonl": b"{}\n",
                }
            )
        )
        hits = ssc.seed_config_violations(seed)
        assert hits == [
            "opt/agentos/deploy/remote/local_mcp_server/config.json#<forbidden-path>",
            "opt/agentos/deploy/remote/local_mcp_server/logs/audit.jsonl#<forbidden-path>",
        ]

    def test_example_json_not_forbidden(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes(
                {"opt/agentos/deploy/remote/local_mcp_server/config.example.json": b"{}\n"}
            )
        )
        assert ssc.seed_config_violations(seed) == []


class TestPublicIpv4InConfig:
    """公网 IPv4 禁入出厂 config 面：保留/私网/环回段放行，可路由公网值违约。"""

    def test_public_ip_reported(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/config/plugins/x/x.yaml": b"url: http://62.1.2.3/e\n"})
        )
        hits = ssc.seed_config_violations(seed)
        assert hits == ["opt/agentos/config/plugins/x/x.yaml#<public-ipv4:62.1.2.3>"]

    def test_reserved_and_private_ips_pass(self, tmp_path: Path) -> None:
        body = "\n".join(
            f"h{i}: {ip}" for i, ip in enumerate(
                ["127.0.0.1", "0.0.0.0", "172.20.0.1", "10.1.2.3",
                 "192.0.2.147", "198.51.100.9", "203.0.113.153", "169.254.1.1"]
            )
        )
        seed = tmp_path / "seed.tar"
        seed.write_bytes(_tar_bytes({"opt/agentos/config/net.yaml": body.encode()}))
        assert ssc.seed_config_violations(seed) == []

    def test_non_config_entries_not_scanned(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/plugins/shared/x/server.py": b"DNS = '8.8.8.8'\n"})
        )
        assert ssc.seed_config_violations(seed) == []

    def test_ipv4_face_scans_md_and_txt_too(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.tar"
        seed.write_bytes(
            _tar_bytes({"opt/agentos/config/users/default/notes.md": b"server 62.9.9.9 down\n"})
        )
        hits = ssc.seed_config_violations(seed)
        assert hits == ["opt/agentos/config/users/default/notes.md#<public-ipv4:62.9.9.9>"]
