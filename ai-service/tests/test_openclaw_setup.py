from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OPENCLAW = ROOT / "openclaw"
VENDOR = OPENCLAW / "vendor" / "agent-notes"
INSTALLER = OPENCLAW / "bootstrap_openclaw.sh"
UNIT = OPENCLAW / "hw1-openclaw.service"
MODEL_UNIT = OPENCLAW / "hw1-openclaw-model.service"
WAIT_MODEL = OPENCLAW / "wait_model_ready.sh"
WRAPPER = OPENCLAW / "hw1-openclaw"
PROBE = ROOT / "tools" / "openclaw" / "openclaw_memory_probe.py"
BENCHMARK_WRAPPER = ROOT / "tools" / "openclaw" / "benchmark_openclaw.sh"
SETUP = ROOT / "setup.sh"

NOTE_TOOLS = [
    "note_write",
    "note_append",
    "note_read",
    "note_tag",
    "note_search",
    "note_list",
    "note_folders",
    "note_move",
    "note_archive",
    "note_backlinks",
]

OPENCLAW_VERSION = "2026.9.2"
OPENCLAW_BYTES = "0"
OPENCLAW_INTEGRITY = "sha512-M6C7UsnX815nv26qBJFYGe6aGzv+ftZLRzV6S9oRXUtXg2Yn67eVntpssT94kgkquKVSeUxerUg0j1ONp4WYQg=="
NODE_VERSION = "24.20.0"
NODE_BYTES = "0"
NODE_SHA256 = "5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7"


def _render_config(path: Path, *, llama_port: int, gateway_port: int = 18789) -> dict:
    text = path.read_text(encoding="utf-8")
    replacements = {
        "@@LLAMA_SERVER@@": json.dumps("/opt/llama.cpp/build/bin/llama-server")[1:-1],
        "@@LLAMA_MODEL@@": json.dumps("/opt/models/model.gguf")[1:-1],
        "@@LLAMA_PORT@@": str(llama_port),
        "@@CONTEXT_TOKENS@@": "16384",
        "@@GATEWAY_PORT@@": str(gateway_port),
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    assert "@@" not in text
    return json.loads(text)


@pytest.fixture(scope="module")
def production_config() -> dict:
    return _render_config(OPENCLAW / "openclaw.json.in", llama_port=18080)


@pytest.fixture(scope="module")
def benchmark_config() -> dict:
    return _render_config(OPENCLAW / "benchmark.json.in", llama_port=18081)


def _assignments(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in source.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(?:\"([^\"]*)\"|'([^']*)'|([^\s#]+))", line)
        if match:
            values[match.group(1)] = next(value for value in match.groups()[1:] if value is not None)
    return values


def _unit_values(source: str, key: str) -> list[str]:
    prefix = f"{key}="
    return [line[len(prefix) :] for line in source.splitlines() if line.startswith(prefix)]


def _manifest() -> dict[Path, str]:
    rows: dict[Path, str] = {}
    for line in (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\s]+)", line)
        assert match, f"invalid SHA256SUMS row: {line!r}"
        relative = Path(match.group(2))
        assert not relative.is_absolute() and ".." not in relative.parts
        assert relative not in rows, f"duplicate SHA256SUMS row: {relative}"
        rows[relative] = match.group(1)
    return rows


def test_installer_inputs_are_present_and_non_symlinked() -> None:
    expected = (
        OPENCLAW / "openclaw.json.in",
        OPENCLAW / "benchmark.json.in",
        UNIT,
        MODEL_UNIT,
        WAIT_MODEL,
        WRAPPER,
        VENDOR / "SHA256SUMS",
        PROBE,
        ROOT / "tools" / "openclaw" / "openclaw_memory_cases.json",
        BENCHMARK_WRAPPER,
    )
    for path in expected:
        assert path.is_file() and not path.is_symlink(), path
    for script in (INSTALLER, WRAPPER, WAIT_MODEL, PROBE, BENCHMARK_WRAPPER):
        assert script.stat().st_mode & 0o111, script
    assert SETUP.is_file() and not SETUP.is_symlink()
    assert SETUP.stat().st_mode & 0o111


def test_rpi_setup_is_one_guided_surface_with_safe_defaults() -> None:
    source = SETUP.read_text(encoding="utf-8")
    assert "Core + OpenClaw" in source
    assert "--mode core|openclaw|openclaw-only" in source
    assert "--openclaw-only" in source
    assert '"$CORE_INSTALLER" "${core_args[@]}"' in source
    assert 'sudo "$OPENCLAW_INSTALLER" "${oc_args[@]}"' in source
    assert "--allow-openclaw-network" in source
    assert "--operator \"$VAULT_OPERATOR\"" in source
    assert "--agent-user hw1-openclaw-agent" in source
    assert "non-interactive setup requires --mode core, --mode openclaw, or --mode openclaw-only" in source


@pytest.mark.parametrize(
    ("template", "llama_port"),
    [("openclaw.json.in", 18080), ("benchmark.json.in", 18081)],
)
def test_templates_render_as_strict_json(template: str, llama_port: int) -> None:
    config = _render_config(OPENCLAW / template, llama_port=llama_port)
    provider = config["models"]["providers"]["hw1local"]

    assert provider["api"] == "openai-completions"
    assert provider["baseUrl"] == f"http://127.0.0.1:{llama_port}/v1"
    if template == "benchmark.json.in":
        local_service = provider["localService"]
        assert provider["apiKey"] == "benchmark-run-injected"
        assert local_service["command"] == "/opt/llama.cpp/build/bin/llama-server"
        assert local_service["healthUrl"] == f"http://127.0.0.1:{llama_port}/health"
        assert local_service["args"][-1] == "--jinja"
        assert ["--host", "127.0.0.1"] == local_service["args"][4:6]
        assert ["--parallel", "1"] in [
            local_service["args"][index : index + 2]
            for index in range(len(local_service["args"]) - 1)
        ]
    else:
        assert "localService" not in provider
        assert provider["apiKey"] == "${HW1_MODEL_API_KEY}"
    model = provider["models"][0]
    assert model["contextWindow"] == model["contextTokens"] == 16384
    assert model["maxTokens"] == 1024


@pytest.mark.parametrize("fixture_name", ["production_config", "benchmark_config"])
def test_configs_expose_only_the_ten_note_tools(request: pytest.FixtureRequest, fixture_name: str) -> None:
    config = request.getfixturevalue(fixture_name)
    tools = config["tools"]
    defaults = config["agents"]["defaults"]

    assert tools["allow"] == NOTE_TOOLS
    assert "alsoAllow" not in tools
    assert tools["codeMode"] is False
    assert tools["experimental"]["planTool"] is False
    assert tools["exec"]["mode"] == "deny"
    assert tools["elevated"]["enabled"] is False
    assert defaults["elevatedDefault"] == "off"
    assert defaults["sandbox"]["mode"] == "off"
    assert defaults["skipBootstrap"] is True
    assert defaults["skills"] == ["notes"]
    assert config["skills"]["load"]["watch"] is False
    assert config["plugins"]["allow"] == ["agent-notes"]
    assert set(config["plugins"]["entries"]) == {"agent-notes"}
    hooks = config["plugins"]["entries"]["agent-notes"]["hooks"]
    assert hooks == {"allowPromptInjection": False, "allowConversationAccess": False}


def test_production_and_benchmark_configs_are_process_isolated(
    production_config: dict, benchmark_config: dict
) -> None:
    prod_defaults = production_config["agents"]["defaults"]
    bench_defaults = benchmark_config["agents"]["defaults"]

    assert prod_defaults["workspace"] == "/var/lib/hw1-openclaw/workspace"
    assert bench_defaults["workspace"] == "/var/lib/hw1-openclaw-bench/workspace"
    assert production_config["agents"]["list"][0]["id"] == "main"
    assert benchmark_config["agents"]["list"][0]["id"] == "bench"
    assert prod_defaults["model"]["primary"] != bench_defaults["model"]["primary"]
    assert production_config["models"]["providers"]["hw1local"]["baseUrl"].endswith(":18080/v1")
    assert benchmark_config["models"]["providers"]["hw1local"]["baseUrl"].endswith(":18081/v1")
    assert "gateway" not in benchmark_config

    gateway = production_config["gateway"]
    assert gateway["mode"] == "local"
    assert gateway["bind"] == "loopback"
    assert gateway["auth"]["mode"] == "token"
    assert gateway["auth"]["token"] == "${OPENCLAW_GATEWAY_TOKEN}"
    assert gateway["terminal"]["enabled"] is False
    assert gateway["tailscale"]["mode"] == "off"
    assert production_config["discovery"]["mdns"]["mode"] == "off"


def test_systemd_unit_runs_unprivileged_with_narrow_write_and_network_access() -> None:
    source = UNIT.read_text(encoding="utf-8")
    active = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

    assert _unit_values(source, "User") == ["@@OPENCLAW_USER@@"]
    assert _unit_values(source, "Group") == ["@@OPENCLAW_USER@@"]
    assert _unit_values(source, "NoNewPrivileges") == ["true"]
    assert _unit_values(source, "CapabilityBoundingSet") == [""]
    assert _unit_values(source, "AmbientCapabilities") == [""]
    assert _unit_values(source, "ProtectSystem") == ["strict"]
    for key in (
        "PrivateDevices",
        "PrivateTmp",
        "ProtectHome",
        "ProtectHostname",
        "ProtectClock",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "RestrictNamespaces",
        "RestrictRealtime",
        "RestrictSUIDSGID",
        "LockPersonality",
        "RemoveIPC",
    ):
        assert _unit_values(source, key) == ["true"]
    assert _unit_values(source, "ProtectProc") == ["invisible"]
    assert _unit_values(source, "DevicePolicy") == ["closed"]
    assert _unit_values(source, "KeyringMode") == ["private"]
    assert _unit_values(source, "LimitCORE") == ["0"]
    assert _unit_values(source, "RestrictAddressFamilies") == ["AF_UNIX AF_INET AF_INET6"]
    assert _unit_values(source, "IPAddressDeny") == ["any"]
    assert _unit_values(source, "IPAddressAllow") == ["localhost"]
    assert _unit_values(source, "StateDirectoryMode") == ["0700"]
    assert _unit_values(source, "CacheDirectoryMode") == ["0700"]
    assert _unit_values(source, "RuntimeDirectoryMode") == ["0700"]
    assert _unit_values(source, "UMask") in (["0077"], ["0007"])
    assert _unit_values(source, "After") == ["network-online.target hw1-openclaw-model.service"]
    assert _unit_values(source, "Wants") == ["network-online.target"]
    assert _unit_values(source, "BindsTo") == ["hw1-openclaw-model.service"]
    assert int(_unit_values(source, "TasksMax")[0]) <= 256
    assert int(_unit_values(source, "LimitNOFILE")[0]) <= 8192
    assert _unit_values(source, "MemoryHigh") and _unit_values(source, "MemoryMax")

    exec_start = _unit_values(source, "ExecStart")
    assert exec_start == [
        "/opt/hw1-openclaw/node/bin/node /opt/hw1-openclaw/current/openclaw.mjs "
        "gateway --port @@GATEWAY_PORT@@"
    ]
    assert "/bin/sh" not in exec_start[0] and "/bin/bash" not in exec_start[0]
    write_paths = set(_unit_values(source, "ReadWritePaths")[0].split())
    assert write_paths == {
        "/var/lib/hw1-openclaw",
        "/var/cache/hw1-openclaw",
        "/run/hw1-openclaw",
        "@@VAULT_PATH@@",
    }
    assert _unit_values(source, "InaccessiblePaths") == ["@@LLAMA_SERVER@@ @@LLAMA_MODEL@@"]
    assert "PrivateNetwork=" not in active
    assert "MemoryDenyWriteExecute=" not in active
    assert "Environment=AGENT_NOTES_DESTRUCTIVE_APPROVALS=1" in source
    assert _unit_values(source, "EnvironmentFile") == [
        "/etc/hw1-openclaw/secrets.env",
        "/etc/hw1-openclaw-model/model.env",
    ]
    assert _unit_values(source, "SupplementaryGroups") == [
        "openclaw-notes openclaw-model-access"
    ]


def test_model_unit_is_separate_supervised_and_authenticated() -> None:
    source = MODEL_UNIT.read_text(encoding="utf-8")

    assert _unit_values(source, "User") == ["openclaw-model"]
    assert _unit_values(source, "Group") == ["openclaw-model"]
    assert _unit_values(source, "SupplementaryGroups") == ["openclaw-model-access"]
    assert _unit_values(source, "PartOf") == ["hw1-openclaw.service"]
    assert _unit_values(source, "Before") == ["hw1-openclaw.service"]
    assert _unit_values(source, "EnvironmentFile") == ["/etc/hw1-openclaw-model/model.env"]
    assert _unit_values(source, "ExecStart") == [
        "@@LLAMA_SERVER@@ --model @@LLAMA_MODEL@@ --alias hw1-openclaw-local "
        "--host 127.0.0.1 --port @@LLAMA_PORT@@ -t 4 -c @@CONTEXT_TOKENS@@ "
        "--parallel 1 --cache-reuse 256 --jinja"
    ]
    start_post = _unit_values(source, "ExecStartPost")
    assert len(start_post) == 1
    assert start_post[0].startswith("/usr/local/libexec/hw1-openclaw/wait-model-ready ")
    assert not start_post[0].startswith("+")
    assert "@@LLAMA_PORT@@ @@CONTEXT_TOKENS@@ 300" in start_post[0]
    assert _unit_values(source, "NoNewPrivileges") == ["true"]
    assert _unit_values(source, "CapabilityBoundingSet") == [""]
    assert _unit_values(source, "ProtectSystem") == ["strict"]
    assert _unit_values(source, "IPAddressDeny") == ["any"]
    assert _unit_values(source, "IPAddressAllow") == ["localhost"]
    assert int(_unit_values(source, "TasksMax")[0]) <= 128
    assert int(_unit_values(source, "MemoryMax")[0].removesuffix("G")) <= 11
    assert "openclaw-notes" not in source

    helper = WAIT_MODEL.read_text(encoding="utf-8")
    assert "MainPID" in helper and "/hw1-openclaw-model.service" in helper
    assert 'grep -Fq "pid=$pid,"' in helper
    assert "/usr/bin/python3 -I -" in helper
    assert "/usr/bin/curl --disable" in helper
    assert 'os.environ["LLAMA_API_KEY"]' in helper
    assert "/tokenize" in helper
    assert "exc.code != 401" in helper


def test_wrapper_cannot_escalate_the_service_account() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "runuser -u @@OPENCLAW_USER@@" in source
    assert "umask 0007" in source
    assert not re.search(r"^\s*(?:exec\s+)?sudo\b", source, re.MULTILINE)
    assert "sudoers" not in source
    assert "AGENT_NOTES_DESTRUCTIVE_APPROVALS=1" in source
    assert "source /etc/hw1-openclaw-model/model.env" in source
    assert "/opt/hw1-openclaw/node/bin/node" in source
    assert "cd -- /var/lib/hw1-openclaw/workspace" in source
    assert "openclaw.mjs \"$@\"" in source


def test_vendored_notes_manifest_is_complete_and_current() -> None:
    rows = _manifest()
    assert set(rows) == {
        Path("SKILL.md"),
        Path("SOURCE.md"),
        Path("LICENSE"),
        Path("plugin/index.js"),
        Path("plugin/openclaw.plugin.json"),
        Path("plugin/package.json"),
        Path("plugin/README.md"),
    }
    for relative, expected in rows.items():
        path = VENDOR / relative
        assert path.is_file() and not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative


def test_plugin_has_stable_result_envelope_and_exact_tool_contract() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")
    manifest = json.loads((VENDOR / "plugin" / "openclaw.plugin.json").read_text(encoding="utf-8"))
    package = json.loads((VENDOR / "plugin" / "package.json").read_text(encoding="utf-8"))

    assert manifest["id"] == "agent-notes"
    assert manifest["contracts"]["tools"] == NOTE_TOOLS
    assert package["name"] == "openclaw-plugin-agent-notes"
    assert package["peerDependencies"]["openclaw"] == ">=2026.9.2 <2026.10.0"
    assert 'from "openclaw/plugin-sdk/tool-results"' in source
    assert "return jsonResult(await t.execute(params ?? {}));" in source
    assert 'api.on("before_tool_call", notesApprovalHook);' in source
    registered = re.findall(r'tool\(\{ name: "(note_[a-z_]+)"', source)
    assert registered == NOTE_TOOLS
    for forbidden in ("node:child_process", "execSync(", "spawn(", "eval(", "fetch("):
        assert forbidden not in source


def test_plugin_approval_bypass_is_guarded_by_disposable_vault_identity() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")

    assert "AGENT_NOTES_VAULT must be an absolute, non-root path" in source
    assert 'process.env.AGENT_NOTES_DESTRUCTIVE_APPROVALS !== "0"' in source
    assert "AGENT_NOTES_BENCHMARK_ROOT" in source
    assert "realpathSync(benchRootRaw)" in source
    assert "realpathSync(VAULT)" in source
    assert 'benchRoot.startsWith("/var/lib/hw1-openclaw-bench/")' in source
    assert "path.relative(benchRoot, vaultReal)" in source
    assert 'rel.startsWith(".." + path.sep)' in source
    assert '.hw1-openclaw-benchmark-vault"' in source
    assert "sentinel.isFile()" in source
    assert "sentinel.isSymbolicLink()" in source
    assert "sentinel.uid !== process.getuid()" in source
    assert "sentinel.mode & 0o077" in source
    archive_gate = source.index('event.toolName === "note_archive"')
    assert source.rfind("if (DESTRUCTIVE_APPROVALS", 0, archive_gate) >= 0


def test_plugin_agent_mutations_are_scoped_below_dedicated_vault_roots() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")
    assert 'const WRITE_BASES = new Set(["reference", "projects", "log", ARCHIVE_BASE])' in source
    assert "function writableAgentId(id)" in source
    assert "agent write scope is limited to reference/, projects/, log/, or archive/" in source
    assert source.count("writableScopeError") >= 4


def test_plugin_archive_collision_suffix_cannot_be_truncated_away() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")

    assert "if (n > 9999)" in source
    assert "flat.slice(0, 100 - suffix.length)" in source
    assert '(flat + "-" + n).slice(0, 100)' not in source


def test_production_full_note_writes_do_not_have_an_existence_check_race() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")
    hook = source[source.index("async function notesApprovalHook") : source.index("const tool =")]
    write_start = hook.index(
        'if (DESTRUCTIVE_APPROVALS && event.toolName === "note_write")'
    )
    move_start = hook.index(
        'if (DESTRUCTIVE_APPROVALS && event.toolName === "note_move")',
        write_start,
    )
    write_gate = hook[write_start:move_start]

    assert "Write complete vault note" in write_gate
    assert "await exists" not in write_gate
    assert "resolveNoteFile" not in write_gate


def test_note_tools_are_sequential_and_cannot_create_hidden_quota_bypasses() -> None:
    source = (VENDOR / "plugin" / "index.js").read_text(encoding="utf-8")

    assert 'executionMode: "sequential"' in source
    assert "note title segments cannot begin with '.'" in source
    assert 'if (e.name.startsWith(".") || e.isSymbolicLink()) continue' in source


def test_installer_has_exact_supply_chain_pins_and_valid_bash() -> None:
    subprocess.run(
        ["bash", "-n", str(INSTALLER), str(WRAPPER), str(BENCHMARK_WRAPPER)],
        check=True,
    )
    source = INSTALLER.read_text(encoding="utf-8")
    values = _assignments(source)

    assert values["OPENCLAW_VERSION"] == OPENCLAW_VERSION
    assert values["OPENCLAW_BYTES"] == OPENCLAW_BYTES
    assert values["OPENCLAW_INTEGRITY"] == OPENCLAW_INTEGRITY
    assert values["NODE_VERSION"] == NODE_VERSION
    assert values["NODE_BYTES"] == NODE_BYTES
    assert values["NODE_SHA256"] == NODE_SHA256
    assert values["DEFAULT_MODEL_BYTES"] == "3676339264"
    assert values["DEFAULT_MODEL_SHA256"] == "d10253b60d9699c4936a024fded42cba4581dc3640182146cba95fe57c143ac6"
    assert values["DEFAULT_LLAMA_REF"] == "b10516"
    assert values["DEFAULT_LLAMA_COMMIT"] == "b95502b"
    assert values["OPENCLAW_URL"] == (
        "https://registry.npmjs.org/openclaw/-/openclaw-${OPENCLAW_VERSION}.tgz"
    )
    assert values["NODE_URL"] == (
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz"
    )
    assert re.fullmatch(r"1\.4\.0-hw1\.\d+", values["NOTES_BUNDLE_VERSION"])
    assert "openclaw@latest" not in source and "npm install -g openclaw" not in source
    assert "sha256sum --check SHA256SUMS" in source
    assert '[[ $model_sha == "$DEFAULT_MODEL_SHA256" ]]' in source
    assert "sha512-sri" in source
    assert '[[ $actual_ref == "$DEFAULT_LLAMA_REF" ]]' in source
    assert 'git -c safe.directory="$DEFAULT_LLAMA_ROOT"' in source
    assert "diff --quiet --ignore-submodules HEAD" in source
    assert '"$SERVER_BIN" --help' not in source
    assert "llama-server must not be set-id or group/world-writable" in source
    assert 'assert_user_cannot_mutate_artifact "$OPENCLAW_USER" "$SERVER_BIN"' in source
    assert 'assert_user_cannot_mutate_artifact "$BENCH_USER" "$MODEL_PATH"' in source
    assert "normalize_immutable_tree" in source
    assert 'find "$root" -xdev -type d -exec chmod 0755 {} +' in source
    assert 'find "$root" -xdev -type f -exec chmod a+r,go-w,u-s,g-s {} +' in source
    assert 'runuser -u "$OPENCLAW_USER" -- test -x "$OPT_ROOT/node/bin/node"' in source
    assert 'runuser -u "$BENCH_USER" -- test -r "$OPT_ROOT/agent-notes/skills/notes/SKILL.md"' in source
    assert "build_openclaw_cli" in source
    assert "provision_default_model" in source
    assert "provision_default_llama" in source
    assert "huggingface.co/$DEFAULT_MODEL_REPO/resolve/$DEFAULT_MODEL_REV" in source
    assert "--recurse-submodules" in source
    assert "cmake --build" in source
    assert "DEFAULT_LLAMA_COMMIT" in source
    assert 'runuser -u "$BUILD_USER" -- env -i' in source
    assert "track_temp_dir" in source
    assert 'rm -rf -- "$temp_dir"' in source
    assert "track_temp_file" in source
    assert 'track_temp_file "$tmp"' in source
    assert 'rm -f -- "$temp_file"' in source
    assert "finalize_hostile_build_tree" in source
    assert 'chown -h root:root "$root"' in source
    assert 'pkill -KILL -u "$build_uid"' in source
    assert 'chown -hR root:root "$root"' in source
    assert "os.O_EXCL" in source and "os.O_NOFOLLOW" in source
    assert "build output contains a special file" in source
    assert source.count('require_openclaw_version "') == 4
    assert r'OpenClaw ([^ ]+)(?: \([0-9a-f]{7}\))?' in source
    assert '$(prod_cli --version)' not in source
    assert '$(bench_cli --version)' not in source


def test_installer_separates_build_benchmark_and_production_identities() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    values = _assignments(source)

    identities = {
        values["OPENCLAW_USER"],
        values["MODEL_USER"],
        values["BENCH_USER"],
        values["BUILD_USER"],
    }
    assert identities == {"openclaw", "openclaw-model", "openclaw-bench", "_openclaw-build"}
    assert "--agent-user" in source
    assert "select_agent_user" in source
    assert "hw1-openclaw-agent" in source
    assert 'ensure_account "$BUILD_USER" "$BUILD_HOME"' in source
    assert 'ensure_account "$MODEL_USER" "$MODEL_HOME"' in source
    assert 'runuser -u "$BUILD_USER" -- env' in source
    assert 'runuser -u "$BENCH_USER" -- env' not in source[source.index("install_runtime()") : source.index("install_notes_bundle()")]
    assert "OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1" in source
    assert "cd -- /var/lib/hw1-openclaw/workspace" in source
    assert "cd -- /var/lib/hw1-openclaw-bench/workspace" in source
    assert 'verify_groups "$BUILD_USER" "$BUILD_USER"' in source
    assert 'verify_groups "$BENCH_USER" "$BENCH_USER"' in source
    assert 'verify_groups "$MODEL_USER" "$MODEL_USER $MODEL_ACCESS_GROUP"' in source
    assert "must have a non-root numeric UID" in source
    assert "UID $uid is shared with another account" in source
    assert "primary GID $gid is shared with another group" in source
    assert 'verify_group_members_exact "$NOTES_GROUP" "$notes_members"' in source
    assert 'verify_group_members_exact "$MODEL_ACCESS_GROUP" "$OPENCLAW_USER $MODEL_USER"' in source
    assert '"$OPENCLAW_USER"|"$MODEL_USER"|"$BENCH_USER"|"$BUILD_USER")' in source


def test_installer_rejects_unsafe_or_unmanaged_vault_paths() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    values = _assignments(source)

    assert values["VAULT_PATH"].startswith("/srv/hw1-openclaw-vaults/")
    assert "os.path.normpath" in source
    assert "must be lexically normalized" in source
    assert "*/../*" in source and "*//*" in source
    assert '[[ $VAULT_PATH == /srv/hw1-openclaw-vaults/* ]]' in source
    assert "--vault must be a direct child of /srv/hw1-openclaw-vaults/" in source
    assert "--adopt-vault" in source
    assert ".hw1-openclaw-managed" in source
    assert "existing vault is unmanaged" in source
    assert 'OPERATOR_USER="${SUDO_USER:-}"' not in source
    assert re.search(r'^OPERATOR_USER=$', source, re.MULTILINE)
    assert '[[ $arch == aarch64 || $arch == arm64 ]] || die' in source
    assert "reject_service_hidden_path --model" in source
    assert "reject_service_hidden_path --server-bin" in source
    for hidden in ("/home/*", "/root/*", "/run/user/*", "/tmp/*", "/var/tmp/*", "/dev/*"):
        assert hidden in source


def test_installer_renders_custom_vault_into_every_installed_wrapper() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    benchmark = BENCHMARK_WRAPPER.read_text(encoding="utf-8")

    assert 'AGENT_NOTES_VAULT=@@VAULT_PATH@@' in WRAPPER.read_text(encoding="utf-8")
    assert 'PRODUCTION_VAULT="@@VAULT_PATH@@"' in benchmark
    assert 'render_text_file "$SCRIPT_DIR/../tools/openclaw/benchmark_openclaw.sh"' in installer
    assert 'render_text_file "$SCRIPT_DIR/hw1-openclaw-model.service"' in installer
    assert 'install_managed_file "$tmp_benchmark_wrapper"' in installer
    assert "/etc/systemd/system/hw1-openclaw-model.service" in installer


def test_installer_preserves_secrets_and_detects_managed_config_drift() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'if [[ ! -e $CONFIG_ROOT/secrets.env && ! -L $CONFIG_ROOT/secrets.env ]]' in source
    assert "existing secrets.env is not a regular non-symlink file" in source
    assert "existing gateway token file has an unexpected format" in source
    assert 're.fullmatch(r"OPENCLAW_GATEWAY_TOKEN=[0-9a-f]{64}\\n", text)' in source
    assert "HW1_MODEL_API_KEY=%s\\nLLAMA_API_KEY=%s\\n" in source
    assert 'match.group(1) != match.group(2)' in source
    assert 'root:$MODEL_ACCESS_GROUP mode 0640' in source
    assert '.hw1-managed-sha256"' in source
    assert "refusing to overwrite locally modified managed file" in source
    assert "recovering interrupted managed-file marker update" in source
    assert "recovering interrupted first managed-file install" in source
    assert 'mv -Tf -- "$destination_tmp" "$destination"' in source
    assert 'mv -Tf -- "$marker_tmp" "$marker"' in source
    assert 'install_managed_file "$tmp_prod" "$CONFIG_ROOT/openclaw.json"' in source
    assert 'install_managed_file "$tmp_bench" "$BENCH_CONFIG_ROOT/openclaw.json"' in source
    assert "verify_existing_managed_unit_for_quiesce hw1-openclaw.service" in source
    assert "verify_existing_managed_unit_for_quiesce hw1-openclaw-model.service" in source
    assert "refusing to stop untracked existing unit" in source


def test_host_hardening_uses_effective_ssh_ports_for_fail2ban_and_ufw() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'mapfile -t ssh_ports < <(sshd -T' in source
    assert "port = %s\\nmaxretry" in source
    assert '"$ports_csv" >"$jail_tmp"' in source
    assert 'run ufw allow "$ssh_port/tcp"' in source


def test_installer_requires_a_live_cross_session_memory_activation() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    enable = source.index("systemctl enable hw1-openclaw.service")
    model_start = source.index("systemctl restart hw1-openclaw-model.service")
    start = source.index("systemctl start hw1-openclaw.service")
    write_turn = source.index('write_session="install-smoke-write-')
    read_turn = source.index('read_session="install-smoke-read-')

    assert enable < model_start < start < write_turn < read_turn
    assert "installed Gateway is stopped and disabled for --no-start staging" in source
    assert "--no-start model unit is unexpectedly active" in source
    assert "skipping systemd path verification because --no-start model/server is not installed" in source
    assert "LIVE_VALIDATION_PENDING=1" in source
    assert "LIVE_VALIDATION_PENDING=0" in source
    assert "systemctl disable --now hw1-openclaw.service" in source
    assert "disabled and stopped the OpenClaw Gateway/model because live validation did not complete" in source
    assert "authenticated model readiness gate did not pass" in source
    assert "Fresh-session memory gate" in source
    assert 'grep -Fxq "activation-code: $secret_nonce" "$note_path"' in source
    assert 'value.get("status") != "ok"' in source
    assert 'not isinstance(result, dict)' in source
    assert 'item.get("fallbackFrom") is not None' in source
    assert 'summary.get("calls") != len(expected_tools)' in source
    assert 'summary.get("tools") != expected_tools' in source
    assert 'summary.get("failures") != 0' in source
    assert '["note_search", "note_folders", "note_append"]' in source
    assert 'texts(read_path, ["note_read"])' in source
    assert "fresh session recovered a value through note_read" in source


def test_installer_parses_diagnostic_json_and_keeps_root_owned_evidence() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "AUDIT_ROOT=/etc/hw1-openclaw/verification" in source
    assert 'install -d -o root -g root -m 0700 "$AUDIT_ROOT"' in source
    assert 'install -o root -g root -m 0600 "$source" "$staged"' in source
    assert 'plugin.get("status") != "loaded"' in source
    assert 'set(tools) != expected_tools' in source
    assert '"before_tool_call" not in' in source
    assert 'policy.get("allowPromptInjection") is not False' in source
    assert 'policy.get("allowConversationAccess") is not False' in source
    assert '"blockedByAllowlist": False' in source
    assert '"blockedByAgentFilter": False' in source
    assert 'summary["critical"] != 0' in source
    assert 'gateway.get("attempted") is not True' in source
    assert 'gateway.get("ok") is not True' in source
    assert "capture_security_audit cold" in source
    assert "capture_security_audit live" in source


def test_benchmark_wrapper_isolated_cleanup_and_resource_validity_contract() -> None:
    source = BENCHMARK_WRAPPER.read_text(encoding="utf-8")

    assert "runuser -u openclaw-bench" in source
    assert "verify_benchmark_identity" in source
    assert 'output="$(bench_runtime "$NODE" "$OPENCLAW" --version' in source
    assert "require_openclaw_version" in source
    assert r'OpenClaw ([^ ]+)(?: \([0-9a-f]{7}\))?' in source
    assert '== "$OPENCLAW_VERSION"' not in source
    assert "systemctl stop hw1-openclaw.service" in source
    assert "systemctl start hw1-openclaw.service" in source
    assert "hw1-openclaw-model.service" in source
    assert "PRODUCTION_MODEL_HEALTH" in source
    assert 'gateway status --require-rpc --json' in source
    assert "failed to restore a healthy hw1-openclaw.service Gateway" in source
    assert "assert_no_hw1_ai_process" in source
    assert "sleep 6" in source
    assert "systemctl is-active --quiet hw1-ai-service.service" not in source
    assert "hw1-ai-service-state.txt" in source
    assert "benchmark UID has read, write, or traversal access" in source
    assert "timeout --signal=TERM --kill-after=30s" in source
    assert 'cd -- "$RUN_DIR"' in source
    assert 'chown root:openclaw-bench "$RUN_DIR"' in source
    assert 'chmod 0710 "$RUN_DIR"' in source
    assert 'PROBE_RUN_DIR="$RUN_DIR/probe"' in source
    assert 'install -d -o openclaw-bench -g openclaw-bench -m 0700 "$PROBE_RUN_DIR"' in source
    assert '--run-dir "$PROBE_RUN_DIR"' in source
    assert 'run_path = probe_dir / "run.json"' in source
    assert 'getattr(os, "O_NOFOLLOW", 0)' in source
    assert "MAX_TEMP_MILLIC=80000" in source
    assert "swap use grew during the run" in source
    assert 'max_swap_used_kib="$(awk -F' in source
    assert "max_swap_used_kib <= swap_used_before_kib" in source
    assert "peak ${max_swap_used_kib}" in source
    assert "openclaw\\.mjs agent" in source
    assert "sticky power/throttle flags are set" in source
    assert "input-sha256.txt" in source
    assert "telemetry.tsv" in source
    assert "rm -rf" not in source


def test_probe_uses_per_run_state_vault_and_guarded_approval_bypass() -> None:
    source = PROBE.read_text(encoding="utf-8")

    assert 'BENCH_ROOT_PREFIX = pathlib.Path("/var/lib/hw1-openclaw-bench")' in source
    assert 'state = repeat_dir / "state"' in source
    assert 'vault = repeat_dir / "vault"' in source
    assert 'sentinel = vault / ".hw1-openclaw-benchmark-vault"' in source
    assert '"AGENT_NOTES_DESTRUCTIVE_APPROVALS": "0"' in source
    assert '"AGENT_NOTES_BENCHMARK_ROOT": str(bench_root)' in source
    assert '"OPENCLAW_STATE_DIR": str(state)' in source
    assert '"OPENCLAW_CONFIG_PATH": str(config)' in source
    assert '"--session-key", session_key' in source
    assert '"--local", "--json"' in source
    assert "agent exec" not in source
    assert "benchmark uid can access production vault" in source
    assert "class ProbeTermination(ProbeError)" in source
    assert "except ProbeTermination:" in source
    assert "except ProcessLookupError:" in source
    assert 'model_env["LLAMA_API_KEY"] = api_key' in source
    assert "model_auth_ready(health_url, api_key)" in source
