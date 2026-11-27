from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
import math
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, build_opener
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
import yaml
import httpx2
from openai import OpenAI
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

app = typer.Typer(help="XueLab 本地优先多智能体工作台")
models_app = typer.Typer(help="下载、切换和启动本机 MLX 大模型")
app.add_typer(models_app, name="models")
console = Console()
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def load_yaml(env_name: str, fallback: str) -> dict[str, Any]:
    path = Path(os.getenv(env_name, fallback))
    if not path.is_absolute():
        path = ROOT / path
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def load_model_config() -> dict[str, Any]:
    path = ROOT / "config" / "model.yaml"
    with path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    overrides_path = ROOT / "data" / "model_overrides.json"
    if overrides_path.is_file():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        for section in ("model", "server", "generation"):
            config.setdefault(section, {}).update(overrides.get(section, {}))
    return config


def model_registry_path() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "models.json"


def read_model_registry() -> dict[str, Any]:
    path = model_registry_path()
    if not path.exists():
        return {"active": None, "models": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_model_registry(registry: dict[str, Any]) -> None:
    model_registry_path().write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def active_model() -> tuple[str, str]:
    """Return (API model id, model path/repo id)."""
    registry = read_model_registry()
    alias = registry.get("active")
    if alias and alias in registry.get("models", {}):
        item = registry["models"][alias]
        model_path = Path(item["local_path"])
        return str(model_path), str(model_path)
    config = load_model_config().get("model", {})
    configured_path = Path(config.get("local_path", ""))
    if configured_path and not configured_path.is_absolute():
        configured_path = ROOT / configured_path
    if configured_path and configured_path.is_dir():
        return str(configured_path), str(configured_path)
    repo_id = os.getenv("MODEL_NAME", config.get("repo_id", "mlx-community/Qwen3-14B-4bit"))
    return repo_id, repo_id


def ask_model(
    system_prompt: str,
    prompt: str,
    history: list[dict[str, str]] | None = None,
    generation_override: dict[str, Any] | None = None,
) -> str:
    config = load_model_config()
    generation = {**config.get("generation", {}), **(generation_override or {})}
    model_id, _ = active_model()
    # Do not send loopback model traffic through the machine's configured proxy.
    started = time.perf_counter()
    with httpx2.Client(trust_env=False, timeout=300) as http_client:
        client = OpenAI(
            base_url=os.getenv("MODEL_BASE_URL", config.get("model", {}).get("base_url", "http://127.0.0.1:8080/v1")),
            api_key=os.getenv("MODEL_API_KEY", "local-only"),
            timeout=300,
            http_client=http_client,
        )
        response = client.chat.completions.create(
            model=os.getenv("MODEL_NAME") or model_id,
            messages=[{"role": "system", "content": system_prompt}, *(history or []), {"role": "user", "content": prompt}],
            temperature=float(generation.get("temperature", 0.3)),
            top_p=float(generation.get("top_p", 0.9)),
            max_tokens=int(generation.get("max_tokens", 2048)),
        )
    elapsed = max(time.perf_counter() - started, 0.001)
    usage = response.usage
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    save_record(
        "model_usage",
        {
            "model": model_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(getattr(usage, "total_tokens", 0) or prompt_tokens + completion_tokens),
            "elapsed_seconds": round(elapsed, 3),
            "generation_tokens_per_second": round(completion_tokens / elapsed, 2),
        },
    )
    return response.choices[0].message.content or ""


def save_record(kind: str, payload: dict[str, Any]) -> None:
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, "created_at": datetime.now(timezone.utc).isoformat(), **payload}
    with (data_dir / "runs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def research_source_warning(text: str, source_text: str = "") -> str | None:
    """Catch common citation/tool markers absent from the user-provided material."""
    patterns = (
        r"(?:\bdoi\s*:|https?://|\b10\.\d{4,9}/)",
        r"\bet\s+al\.?\b",
        r"\b(?:19|20)\d{2}\b",
        r"\b\d+(?:\.\d+)?\s?%",
        r"\b[A-Z]{2,}-\d+(?:\.\d+)*\b",
        r"\b(?:Tobii|Konica\s+Minolta|G\*Power|Stroop(?:\s+(?:Test|Color))?|Sound\s+Level\s+Meter)\b",
    )
    suspicious = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            marker = match.group(0)
            if marker.casefold() not in source_text.casefold():
                suspicious.append(marker)
    if suspicious:
        return (
            "本阶段草稿已被来源检查拦截：模型输出出现了输入材料中没有的年份、引用标记、"
            "百分比或具体测量工具。当前没有连接论文/PDF/学术检索来源，因此不展示该草稿。"
            "请先提供原文或接入检索工具，再生成可核查的科研摘要；目前可以继续讨论不含文献事实的研究设计框架。"
        )
    return None


def conversation_db() -> sqlite3.Connection:
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "harness.sqlite3")
    db.execute(
        "CREATE TABLE IF NOT EXISTS conversation "
        "(session_id TEXT, role TEXT, content TEXT, created_at TEXT)"
    )
    return db


def recent_conversation(session_id: str, limit: int = 12) -> list[dict[str, str]]:
    db = conversation_db()
    try:
        rows = db.execute(
            "SELECT role, content FROM conversation WHERE session_id = ? "
            "ORDER BY rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    finally:
        db.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def store_exchange(session_id: str, prompt: str, answer: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    db = conversation_db()
    try:
        db.executemany(
            "INSERT INTO conversation (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [
                (session_id, "user", prompt, timestamp),
                (session_id, "assistant", answer, timestamp),
            ],
        )
        db.commit()
    finally:
        db.close()


def configured_file_roots() -> dict[str, Path]:
    config_path = ROOT / "config" / "file_roots.yaml"
    with config_path.open("r", encoding="utf-8") as stream:
        raw_roots = (yaml.safe_load(stream) or {}).get("roots", {})
    roots = {
        alias: Path(path).expanduser().resolve()
        for alias, path in raw_roots.items()
    }
    override_path = ROOT / "data" / "file_roots_override.json"
    if override_path.is_file():
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
        for alias in overrides.get("remove", []):
            roots.pop(alias, None)
        for alias, path in overrides.get("add", {}).items():
            roots[alias] = Path(path).expanduser().resolve()
    return roots


def resolve_shared_path(argument: str) -> tuple[str, Path, Path]:
    """Resolve alias:relative/path and prevent reads outside configured roots."""
    alias, separator, relative = argument.partition(":")
    roots = configured_file_roots()
    if not separator or alias not in roots:
        raise ValueError(f"路径格式应为 目录别名:相对路径。可用目录：{', '.join(roots)}")
    root = roots[alias]
    if not root.is_dir():
        raise ValueError(f"配置目录不存在或不可读：{alias}")
    if not relative or relative.strip() in {".", "/"}:
        candidate = root
    else:
        candidate = (root / relative).resolve(strict=True)
    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError("该路径超出配置的共享目录，已拒绝访问")
    if not candidate.exists():
        raise ValueError("找不到这个文件或目录")
    return alias, root, candidate


def file_transfer_limits() -> tuple[int, int, int]:
    config_path = ROOT / "config" / "file_roots.yaml"
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    return (
        int(os.getenv("MAX_FILE_MB") or config.get("max_upload_mb", 100)) * 1024 * 1024,
        int(config.get("max_uncompressed_mb", 1024)) * 1024 * 1024,
        int(config.get("max_files_per_archive", 5000)),
    )


def is_sensitive_transfer_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
        or path.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}
    )


def list_shared_files(argument: str | None) -> str:
    roots = configured_file_roots()
    if not argument:
        return "可用目录别名：" + ", ".join(f"{key}:" for key in roots)
    alias, _, path = resolve_shared_path(argument)
    if not path.is_dir():
        relative = path.relative_to(roots[alias]).as_posix()
        return f"{alias}:{relative} 是文件；可用‘发送 {alias}:{relative}’取回。"
    entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    if not entries:
        return "这个目录为空。"
    lines = []
    for entry in entries[:100]:
        suffix = "/" if entry.is_dir() else ""
        relative = entry.relative_to(roots[alias]).as_posix()
        lines.append(f"{alias}:{relative}{suffix}")
    extra = f"\n…还有 {len(entries) - 100} 项" if len(entries) > 100 else ""
    return "\n".join(lines) + extra


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-5000:]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            continue
    return records


def model_api_status() -> tuple[bool, str | None]:
    config = load_model_config()
    base_url = os.getenv("MODEL_BASE_URL", config.get("model", {}).get("base_url", "http://127.0.0.1:8080/v1"))
    url = base_url.rstrip("/") + "/models"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=1.5) as response:
            payload = json.loads(response.read())
        models = payload.get("data", [])
        return True, str(models[0].get("id")) if models else None
    except Exception:
        return False, None


def file_root_status() -> list[dict[str, Any]]:
    result = []
    override_path = ROOT / "data" / "file_roots_override.json"
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.is_file() else {}
    custom_aliases = set(overrides.get("add", {}))
    for alias, path in configured_file_roots().items():
        state = "可访问"
        try:
            path.stat()
            if not path.is_dir():
                state = "不是目录"
            else:
                with os.scandir(path) as entries:
                    next(entries, None)
        except PermissionError:
            state = "macOS 未授权"
        except FileNotFoundError:
            state = "目录不存在"
        except OSError as exc:
            state = f"不可读：{exc.strerror or '系统错误'}"
        result.append({"alias": alias, "path": str(path), "state": state, "accessible": state == "可访问", "custom": alias in custom_aliases})
    return result


def dashboard_snapshot() -> dict[str, Any]:
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    now = datetime.now().astimezone()
    usage = []
    for row in read_jsonl(data_dir / "runs.jsonl"):
        if row.get("kind") != "model_usage":
            continue
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).astimezone()
        except (KeyError, ValueError):
            continue
        row["_created_local"] = created
        usage.append(row)
    current_month = now.strftime("%Y-%m")
    this_month_usage = [row for row in usage if row["_created_local"].strftime("%Y-%m") == current_month]
    prompt_tokens = sum(int(row.get("prompt_tokens", 0)) for row in this_month_usage)
    completion_tokens = sum(int(row.get("completion_tokens", 0)) for row in this_month_usage)
    usage_days: dict[str, dict[str, int]] = {}
    for row in usage:
        key = row["_created_local"].strftime("%m-%d")
        bucket = usage_days.setdefault(key, {"prompt": 0, "completion": 0})
        bucket["prompt"] += int(row.get("prompt_tokens", 0))
        bucket["completion"] += int(row.get("completion_tokens", 0))
    last_usage = usage[-1] if usage else {}

    cloud_policy = yaml.safe_load((ROOT / "config" / "cloud_policy.yaml").read_text(encoding="utf-8")) or {}
    cloud_override_path = data_dir / "cloud_policy_override.json"
    if cloud_override_path.is_file():
        cloud_policy.update(json.loads(cloud_override_path.read_text(encoding="utf-8")))
    cloud_rows = []
    for row in read_jsonl(data_dir / "cloud_jobs.jsonl"):
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).astimezone()
        except (KeyError, ValueError):
            continue
        if created.strftime("%Y-%m") == current_month:
            cloud_rows.append(row)
    cloud_spend = round(sum(float(row.get("cost_usd", 0)) for row in cloud_rows), 4)

    registry = read_model_registry()
    config = load_model_config()
    model_rows = []
    for alias, model in registry.get("models", {}).items():
        model_rows.append({
            "alias": alias,
            "repo_id": model.get("repo_id", ""),
            "local_path": model.get("local_path", ""),
            "present": Path(model.get("local_path", "")).is_dir(),
            "active": alias == registry.get("active"),
        })
    running, running_id = model_api_status()

    module_rows = []
    for name in ("daily_chat", "research", "video", "hot_capture"):
        module = yaml.safe_load((ROOT / "config" / "modules" / f"{name}.yaml").read_text(encoding="utf-8")).get("module", {})
        module_overrides_path = data_dir / "module_overrides.json"
        module_overrides = json.loads(module_overrides_path.read_text(encoding="utf-8")) if module_overrides_path.is_file() else {}
        module_rows.append({"id": name, "title": module.get("title", name), "status": module.get("status", "unknown"), "capabilities": module.get("capabilities", module.get("capabilities_planned", [])), "generation": {**module.get("generation", config.get("generation", {})), **module_overrides.get(name, {})}})

    token_present = bool(os.getenv("SLACK_BOT_TOKEN")) and bool(os.getenv("SLACK_APP_TOKEN"))
    allowed_slack_users = [x.strip() for x in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",") if x.strip()]
    cached_dms = remembered_slack_dm_channels()
    return {
        "model": {
            "api_running": running,
            "api_model": running_id,
            "selected_alias": registry.get("active"),
            "selected_model": active_model()[0],
            "models": model_rows,
            "generation": config.get("generation", {}),
            "server": config.get("server", {}),
        },
        "usage": {
            "month": current_month,
            "requests": len(this_month_usage),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "last_tokens_per_second": last_usage.get("generation_tokens_per_second", 0),
            "last_elapsed_seconds": last_usage.get("elapsed_seconds", 0),
            "days": list(usage_days.items())[-14:],
        },
        "modules": module_rows,
        "cloud": {
            "enabled": bool(cloud_policy.get("enabled", False)),
            "provider": cloud_policy.get("provider", "runpod"),
            "monthly_budget_usd": float(cloud_policy.get("monthly_budget_usd", 10)),
            "per_job_budget_usd": float(cloud_policy.get("per_job_budget_usd", 2)),
            "spent_month_usd": cloud_spend,
            "jobs_month": len(cloud_rows),
            "dispatch_ready": False,
            "ledger": cloud_rows[-10:],
        },
        "files": {"roots": file_root_status(), "max_upload_mb": file_transfer_limits()[0] // (1024 * 1024)},
        "slack": {"configured": token_present, "allowed_users": len(allowed_slack_users), "dm_ready": any(user in cached_dms for user in allowed_slack_users)},
    }


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def remembered_slack_dm_channels() -> dict[str, str]:
    path = ROOT / "data" / "slack_dm_channels.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def remember_slack_dm_channel(user_id: str, channel_id: str) -> None:
    allowed = {x.strip() for x in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",") if x.strip()}
    if not user_id or user_id not in allowed or not channel_id:
        return
    channels = remembered_slack_dm_channels()
    channels[user_id] = channel_id
    save_json(ROOT / "data" / "slack_dm_channels.json", channels)


def create_archive(arguments: list[str]) -> Path:
    upload_limit, uncompressed_limit, file_limit = file_transfer_limits()
    resolved = [resolve_shared_path(argument) for argument in arguments]
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    export_dir = data_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    archive_path = export_dir / f"xuelab_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_count = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for alias, root, selected in resolved:
                candidates = [selected] if selected.is_file() else selected.rglob("*")
                for candidate in candidates:
                    if not candidate.is_file():
                        continue
                    if is_sensitive_transfer_path(candidate):
                        continue
                    safe_file = candidate.resolve(strict=True)
                    if safe_file != root and not safe_file.is_relative_to(root):
                        continue
                    file_count += 1
                    total_bytes += safe_file.stat().st_size
                    if file_count > file_limit:
                        raise ValueError(f"文件数超过上限 {file_limit}，请缩小范围")
                    if total_bytes > uncompressed_limit:
                        raise ValueError(f"原始内容超过 {uncompressed_limit // (1024 * 1024)} MB 上限，请拆分打包")
                    relative = safe_file.relative_to(root).as_posix()
                    archive.write(safe_file, arcname=f"{alias}/{relative}")
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    if file_count == 0:
        archive_path.unlink(missing_ok=True)
        raise ValueError("所选范围内没有可打包的普通文件")
    if archive_path.stat().st_size > upload_limit:
        archive_path.unlink(missing_ok=True)
        raise ValueError(f"压缩包超过 Slack 上传上限 {upload_limit // (1024 * 1024)} MB，请缩小范围")
    return archive_path


def handle_file_command(event: dict[str, Any], say: Any, client: Any) -> bool:
    """Handle explicit file list/send/zip requests from an allowlisted user."""
    text = event.get("text", "").strip()
    if text.startswith("<@"):
        text = " ".join(part for part in text.split() if not part.startswith("<@"))
    try:
        parts = shlex.split(text)
    except ValueError:
        say("命令里的引号没有配对。请用 帮助 查看文件命令格式。")
        return True
    if not parts:
        return False
    upload_thread_ts = event.get("thread_ts") or (
        event.get("ts") if event.get("channel_type") != "im" else None
    )
    command = parts[0].lower()
    if command in {"帮助", "help", "菜单"}:
        say(
            "我可以聊天，也可回传共享目录文件。\n"
            "文件列表 [别名:路径] — 查看允许目录/目录内容\n"
            "发送 别名:文件路径 — 回传单个文件\n"
            "打包 别名:目录 [别名:文件]… — ZIP 后回传；路径支持用引号包住空格。\n"
            "例：文件列表 codex:；发送 downloads:论文.pdf；打包 videos:剪辑项目"
        )
        return True
    if command in {"文件列表", "ls"}:
        try:
            say(list_shared_files(parts[1] if len(parts) > 1 else None))
        except (ValueError, OSError) as exc:
            say(str(exc))
        return True
    if command in {"发送", "send"}:
        if len(parts) != 2:
            say("格式：发送 目录别名:相对文件路径")
            return True
        try:
            _, _, path = resolve_shared_path(parts[1])
            if not path.is_file():
                raise ValueError("发送命令只接受单个文件；目录请使用‘打包’")
            if is_sensitive_transfer_path(path):
                raise ValueError("出于安全考虑，.env、密钥和证书文件不能通过 Slack 回传")
            upload_limit, _, _ = file_transfer_limits()
            if path.stat().st_size > upload_limit:
                raise ValueError(f"文件超过 {upload_limit // (1024 * 1024)} MB 上传上限；可试用‘打包’或调低/调整配置")
            client.files_upload_v2(
                file=str(path),
                channel=event["channel"],
                title=path.name,
                initial_comment=f"从本机共享目录回传：{parts[1]}",
                thread_ts=upload_thread_ts,
            )
        except Exception as exc:
            say(f"文件回传失败：{exc}")
        return True
    if command in {"打包", "zip"}:
        if len(parts) < 2:
            say("格式：打包 目录别名:目录或文件 [目录别名:另一个文件]…")
            return True
        say("收到，我在本机整理并打包这些内容。")
        archive_path: Path | None = None
        try:
            archive_path = create_archive(parts[1:])
            client.files_upload_v2(
                file=str(archive_path),
                channel=event["channel"],
                title=archive_path.name,
                initial_comment="本地已打包完成，ZIP 文件已回传到此私聊。",
                thread_ts=upload_thread_ts,
            )
        except Exception as exc:
            say(f"打包/回传失败：{exc}")
        finally:
            if archive_path:
                archive_path.unlink(missing_ok=True)
        return True
    return False


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="要处理的问题"),
    role: str = typer.Option("daily_assistant", help="config/agents.yaml 中的角色名"),
) -> None:
    """调用本机 OpenAI 兼容模型，以指定专家角色回答。"""
    agents = load_yaml("AGENTS_FILE", "config/agents.yaml").get("agents", {})
    if role not in agents:
        raise typer.BadParameter(f"未知角色 {role}；可用角色：{', '.join(agents)}")
    agent = agents[role]
    system_prompt = f"你是{agent['title']}。风格：{agent['style']}\n\n{agent['instructions']}"
    answer = ask_model(system_prompt, prompt)
    console.print(Markdown(answer))
    save_record("ask", {"role": role, "prompt": prompt, "answer": answer})


@app.command()
def run(
    module: str = typer.Argument(..., help="独立模块：research 或 video"),
    prompt: str = typer.Argument(..., help="工作流输入"),
) -> None:
    """只运行指定模块；科研和短视频不会互相串联。"""
    if module not in {"research", "video"}:
        raise typer.BadParameter("该命令只接受 research 或 video；日常对话和热点捕获是独立模块")
    module_path = ROOT / "config" / "modules" / f"{module}.yaml"
    with module_path.open("r", encoding="utf-8") as stream:
        module_config = (yaml.safe_load(stream) or {}).get("module", {})
    module_override_path = ROOT / "data" / "module_overrides.json"
    if module_override_path.is_file():
        module_config["generation"] = {
            **module_config.get("generation", {}),
            **json.loads(module_override_path.read_text(encoding="utf-8")).get(module, {}),
        }
    agents = load_yaml("AGENTS_FILE", "config/agents.yaml").get("agents", {})
    workflows = module_config.get("workflows", {})
    workflow_name = "paper_brief" if module == "research" else "short_video"
    if module_config.get("status") != "active":
        console.print(f"[yellow]{module_config.get('title', module)}模块目前是配置骨架，当前只提供角色草稿能力。[/yellow]")
    if workflow_name not in workflows:
        raise typer.BadParameter(f"{module} 模块没有配置工作流 {workflow_name}")
    steps = workflows[workflow_name]["steps"]
    context = f"任务：{prompt}\n\n请完成你的阶段产物："
    transcript = []
    for index, step in enumerate(steps, start=1):
        role = step["agent"]
        agent = agents[role]
        console.print(f"[bold cyan]{module_config.get('title')} · 阶段 {index}/{len(steps)}：{agent['title']}[/bold cyan]")
        if (
            module == "research"
            and transcript
            and transcript[-1]["answer"].startswith("本阶段草稿已被来源检查拦截")
        ):
            answer = "审校已暂停：上一阶段没有可审查的来源支持内容。请提供论文原文/PDF或连接学术检索工具后重试。"
            transcript.append({"role": role, "expected_output": step["output"], "answer": answer})
            console.print(Markdown(answer))
            context += f"\n\n上一阶段（{agent['title']}）结果：\n{answer}"
            continue
        source_guard = ""
        if module == "research":
            source_guard = (
                "\n\n科研来源硬约束：只能依据用户任务中实际提供的材料；当前没有已连接的 PDF/网页检索工具。"
                "没有原文依据时，禁止写作者姓名、年份、DOI/URL、期刊名、量化结果、样本量或设备型号；"
                "改写为‘待检索/待核实’。可以提出研究设计建议，但须标为建议而不是已发表事实。"
            )
        answer = ask_model(
            f"你是{agent['title']}。风格：{agent['style']}\n\n{agent['instructions']}{source_guard}",
            context,
            generation_override=module_config.get("generation"),
        )
        if module == "research":
            warning = research_source_warning(answer, source_text=prompt)
            if warning:
                answer = warning
        transcript.append({"role": role, "expected_output": step["output"], "answer": answer})
        context += f"\n\n上一阶段（{agent['title']}）结果：\n{answer}\n\n你的阶段目标：{step['output']}"
        console.print(Markdown(answer))
    save_record("module_run", {"module": module, "workflow": workflow_name, "prompt": prompt, "steps": transcript})


@app.command("modules")
def list_modules() -> None:
    """显示四个互相独立的功能模块及当前接入状态。"""
    for module_name in ("daily_chat", "research", "video", "hot_capture"):
        path = ROOT / "config" / "modules" / f"{module_name}.yaml"
        with path.open("r", encoding="utf-8") as stream:
            module = (yaml.safe_load(stream) or {}).get("module", {})
        status = module.get("status", "unknown")
        console.print(f"[bold]{module.get('title', module_name)}[/bold] · {module_name} · {status}")
        console.print(f"  {', '.join(module.get('capabilities', module.get('capabilities_planned', [])))}")


@models_app.command("list")
def list_models() -> None:
    """列出本地模型库和当前使用模型。"""
    registry = read_model_registry()
    active = registry.get("active")
    if not registry.get("models"):
        config = load_model_config().get("model", {})
        console.print("本地模型库尚无已导入模型。")
        console.print(f"配置的默认模型：{config.get('repo_id')}（可用 models add 导入）")
        return
    for alias, item in registry["models"].items():
        marker = "[green]当前[/green]" if alias == active else ""
        present = Path(item["local_path"]).is_dir()
        state = "已下载" if present else "目录缺失"
        console.print(f"{alias} · {item['repo_id']} · {state} {marker}")


@models_app.command("add")
def add_model(
    repo_id: str = typer.Argument(..., help="Hugging Face MLX 模型仓库 ID，例如 mlx-community/Qwen3-14B-4bit"),
    alias: str | None = typer.Option(None, "--alias", help="本地短名称；不填时从仓库名自动生成"),
) -> None:
    """下载一个 MLX 格式模型并加入本地模型库。模型可能有数 GB。"""
    from huggingface_hub import snapshot_download

    chosen_alias = alias or repo_id.split("/")[-1].lower().replace(".", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", chosen_alias):
        raise typer.BadParameter("alias 只能使用小写英文字母、数字、短横线和下划线")
    target = ROOT / "models" / chosen_alias
    if target.exists() and any(target.iterdir()):
        if not typer.confirm(f"目录 {target} 已有文件；继续更新/补齐该模型？"):
            raise typer.Exit(1)
    else:
        console.print(f"准备下载：{repo_id}\n本地保存：{target}\n模型仓库可能超过数 GB。请确认模型卡中的许可证和 MLX-LM 兼容性。")
        if not typer.confirm("开始下载？"):
            raise typer.Exit(1)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo_id, local_dir=str(target))
    registry = read_model_registry()
    registry.setdefault("models", {})[chosen_alias] = {
        "repo_id": repo_id,
        "local_path": str(target.resolve()),
    }
    write_model_registry(registry)
    console.print(f"模型已导入。切换使用：xue-harness models use {chosen_alias}")


@models_app.command("use")
def use_model(alias: str = typer.Argument(..., help="models list 显示的模型短名称")) -> None:
    """将一个已下载模型设为日常和 Slack 助理使用的模型。"""
    registry = read_model_registry()
    if alias not in registry.get("models", {}):
        raise typer.BadParameter("没有这个本地模型；先运行 xue-harness models list 或 models add")
    model_path = Path(registry["models"][alias]["local_path"])
    if not model_path.is_dir():
        raise typer.BadParameter(f"模型目录不存在：{model_path}")
    registry["active"] = alias
    write_model_registry(registry)
    console.print(f"已切换到 {alias}。若模型服务已经运行，请先停止并用 xue-harness models serve 重启。")


@models_app.command("serve")
def serve_model() -> None:
    """启动当前模型的本机 MLX API 服务（仅监听 127.0.0.1）。"""
    venv_command = ROOT / ".venv" / "bin" / "mlx_lm.server"
    command = str(venv_command) if venv_command.exists() else shutil.which("mlx_lm.server")
    if not command:
        raise typer.BadParameter("找不到 mlx_lm.server；请先在当前项目虚拟环境安装 mlx-lm")
    _, model_path = active_model()
    settings = load_model_config().get("model", {})
    port = int(settings.get("server_port", 8080))
    server = load_model_config().get("server", {})
    command_args = [command, "--model", model_path, "--host", settings.get("server_host", "127.0.0.1"), "--port", str(port)]
    command_args.extend(["--decode-concurrency", str(server.get("decode_concurrency", 1))])
    command_args.extend(["--prompt-concurrency", str(server.get("prompt_concurrency", 1))])
    command_args.extend(["--prefill-step-size", str(server.get("prefill_step_size", 1024))])
    kv_bits = server.get("kv_cache_bits")
    if kv_bits is not None:
        command_args.extend(["--kv-bits", str(kv_bits)])
    console.print(f"正在启动本地模型：{model_path}\n监听地址：127.0.0.1:{port}")
    subprocess.run(command_args, check=False)


@app.command()
def serve_slack() -> None:
    """启动 Slack Socket Mode 对话入口；只响应配置白名单中的用户。"""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    if (ROOT / ".env.txt").exists():
        console.print(
            "[yellow]检测到 .env.txt：程序只读取项目目录中的 .env。macOS 文本编辑器可能自动追加 .txt；"
            "请用终端编辑正确的 .env 文件。[/yellow]"
        )

    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    allowed_users = {
        user_id.strip()
        for user_id in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",")
        if user_id.strip()
    }
    if not bot_token or not app_token:
        raise typer.BadParameter("请先在 .env 配置 SLACK_BOT_TOKEN 与 SLACK_APP_TOKEN")
    if not allowed_users:
        raise typer.BadParameter("请在 .env 的 SLACK_ALLOWED_USER_IDS 配置自己的 Slack 用户 ID")

    slack_app = App(token=bot_token)

    def answer_event(event: dict[str, Any], say: Any, *, remove_mentions: bool) -> None:
        user_id = event.get("user", "")
        if user_id not in allowed_users:
            return
        if event.get("channel_type") == "im":
            remember_slack_dm_channel(user_id, str(event.get("channel", "")))
        if handle_file_command(event, say, slack_app.client):
            return
        text = event.get("text", "")
        prompt = (
            " ".join(part for part in text.split() if not part.startswith("<@"))
            if remove_mentions
            else text
        )
        reply_context = {"thread_ts": event.get("ts")} if remove_mentions else {}
        if not prompt.strip():
            say("我在。发一个问题给我即可。", **reply_context)
            return
        try:
            agents = load_yaml("AGENTS_FILE", "config/agents.yaml").get("agents", {})
            role = agents["daily_assistant"]
            session_id = (
                f"{event.get('channel', '')}:{event.get('thread_ts') or event.get('ts', '')}"
                if remove_mentions
                else f"{event.get('channel', '')}:direct"
            )
            answer = ask_model(
                f"你是{role['title']}。风格：{role['style']}\n\n{role['instructions']}",
                prompt,
                history=recent_conversation(
                    session_id,
                    int(load_model_config().get("generation", {}).get("conversation_history_messages", 12)),
                ),
            )
            store_exchange(session_id, prompt, answer)
            save_record("slack_ask", {"user_id": user_id, "prompt": prompt, "answer": answer})
            say(answer[:39000], **reply_context)
        except Exception:
            console.print_exception()
            say("本地模型暂时没有完成请求，请稍后再试。", **reply_context)

    @slack_app.event("app_mention")
    def on_mention(event: dict[str, Any], say: Any, ack: Any) -> None:
        ack()
        answer_event(event, say, remove_mentions=True)

    @slack_app.event("message")
    def on_direct_message(event: dict[str, Any], say: Any, ack: Any) -> None:
        ack()
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        answer_event(event, say, remove_mentions=False)

    console.print("[green]Slack Socket Mode 已启动；仅处理白名单用户的私聊和 @提及。[/green]")
    SocketModeHandler(slack_app, app_token).start()


@app.command("serve-web")
def serve_web(
    port: int = typer.Option(8090, help="本机网页端口；仅监听 127.0.0.1"),
) -> None:
    """启动仅供本机浏览器访问的日常对话网页。"""
    html_path = ROOT / "web" / "index.html"
    if not html_path.is_file():
        raise typer.BadParameter(f"找不到网页文件：{html_path}")
    agents = load_yaml("AGENTS_FILE", "config/agents.yaml").get("agents", {})
    role = agents["daily_assistant"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 * 1024:
                raise ValueError("请求内容为空或超过 1 MB")
            origin = self.headers.get("Origin")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if origin and origin != expected_origin:
                raise ValueError("已拒绝跨站请求")
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in {"127.0.0.1", "localhost"}:
                raise ValueError("网页控制台只允许本机访问")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("请求数据格式错误")
            return value

        def _send_to_slack(self, file_path: Path, comment: str) -> None:
            from slack_sdk import WebClient
            bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
            allowed = [x.strip() for x in os.getenv("SLACK_ALLOWED_USER_IDS", "").split(",") if x.strip()]
            if not bot_token or not allowed:
                raise ValueError("请先在 .env 设置真实的 Slack Bot Token 和成员 ID")
            client = WebClient(token=bot_token)
            channel = remembered_slack_dm_channels().get(allowed[0])
            if not channel:
                raise ValueError("尚未记住你的 Slack 私聊频道。先启动 serve-slack，并在 Slack 里主动给机器人发一条私聊消息，再回到这里发送文件。")
            response = client.files_upload_v2(
                file=str(file_path), channel=channel, title=file_path.name,
                initial_comment=comment[:1500],
            )
            if not response.get("ok", False):
                raise ValueError("Slack 文件上传未成功")

        def _handle_api_post(self, route: str) -> None:
            body = self._body()
            if route == "/api/chat":
                prompt = str(body.get("message", "")).strip()
                if not prompt or len(prompt) > 12_000:
                    raise ValueError("请输入问题（最多 12000 字符）")
                history = body.get("history", [])
                if not isinstance(history, list):
                    raise ValueError("对话记录格式错误")
                safe_history = [
                    {"role": item["role"], "content": item["content"][:12_000]}
                    for item in history[-12:]
                    if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), str)
                ]
                answer = ask_model(
                    f"你是{role['title']}。风格：{role['style']}\n\n{role['instructions']}",
                    prompt, history=safe_history,
                )
                self._json(200, {"answer": answer})
                return

            if route == "/api/settings":
                module_name = str(body.get("module", "daily_chat"))
                if module_name not in {"daily_chat", "research", "video"}:
                    raise ValueError("只可调整日常对话、科研和视频模块的生成参数")
                current = load_model_config()
                module_generation = module_name in {"research", "video"}
                if module_generation:
                    module_path = ROOT / "config" / "modules" / f"{module_name}.yaml"
                    module_data = yaml.safe_load(module_path.read_text(encoding="utf-8")).get("module", {})
                    module_override_path = ROOT / "data" / "module_overrides.json"
                    module_overrides = json.loads(module_override_path.read_text(encoding="utf-8")) if module_override_path.exists() else {}
                    generation = {**module_data.get("generation", current.get("generation", {})), **module_overrides.get(module_name, {})}
                    server_cfg = dict(current.get("server", {}))
                else:
                    generation = dict(current.get("generation", {}))
                    server_cfg = dict(current.get("server", {}))
                for key, low, high, cast in (
                    ("temperature", 0.0, 2.0, float),
                    ("top_p", 0.01, 1.0, float),
                    ("max_tokens", 64, 8192, int),
                    ("conversation_history_messages", 0, 40, int),
                ):
                    if key in body:
                        value = cast(body[key])
                        if not low <= value <= high:
                            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
                        generation[key] = value
                for key, low, high in (("decode_concurrency", 1, 4), ("prompt_concurrency", 1, 4), ("prefill_step_size", 128, 8192)):
                    if key in body:
                        value = int(body[key])
                        if not low <= value <= high:
                            raise ValueError(f"{key} 超出允许范围 {low}–{high}")
                        server_cfg[key] = value
                if "kv_cache_bits" in body:
                    value = body["kv_cache_bits"]
                    if value not in (None, 4, 8):
                        raise ValueError("KV 缓存位宽只支持不量化、4-bit 或 8-bit")
                    server_cfg["kv_cache_bits"] = value
                if module_generation:
                    module_override_path = ROOT / "data" / "module_overrides.json"
                    module_overrides[module_name] = generation
                    save_json(module_override_path, module_overrides)
                    restart_required = False
                else:
                    overrides_path = ROOT / "data" / "model_overrides.json"
                    old = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
                    restart_required = server_cfg != old.get("server", current.get("server", {}))
                    old.update({"generation": generation, "server": server_cfg})
                    save_json(overrides_path, old)
                self._json(200, {"ok": True, "module": module_name, "settings": {"generation": generation, "server": server_cfg}, "restart_required": restart_required})
                return

            if route == "/api/models/use":
                alias = str(body.get("alias", ""))
                registry = read_model_registry()
                item = registry.get("models", {}).get(alias)
                if not item or not Path(item.get("local_path", "")).is_dir():
                    raise ValueError("模型不存在或本地模型目录缺失")
                registry["active"] = alias
                write_model_registry(registry)
                self._json(200, {"ok": True, "alias": alias, "restart_required": True})
                return

            if route == "/api/models/add":
                repo_id = str(body.get("repo_id", "")).strip()
                alias = str(body.get("alias", "")).strip().lower()
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id):
                    raise ValueError("请填写 Hugging Face 仓库 ID，例如 mlx-community/Qwen3-8B-4bit")
                alias = alias or repo_id.split("/")[-1].lower().replace(".", "-")
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", alias):
                    raise ValueError("模型别名仅可用小写字母、数字、下划线和短横线")
                from huggingface_hub import snapshot_download
                target = ROOT / "models" / alias
                target.mkdir(parents=True, exist_ok=True)
                snapshot_download(repo_id=repo_id, local_dir=str(target))
                registry = read_model_registry()
                registry.setdefault("models", {})[alias] = {"repo_id": repo_id, "local_path": str(target.resolve())}
                write_model_registry(registry)
                self._json(200, {"ok": True, "alias": alias, "repo_id": repo_id, "select_required": True})
                return

            if route == "/api/files/roots":
                alias, action = str(body.get("alias", "")).strip(), str(body.get("action", "add"))
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", alias):
                    raise ValueError("目录别名以小写字母开头，只能包含小写字母、数字、_、-")
                override_path = ROOT / "data" / "file_roots_override.json"
                overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {"add": {}, "remove": []}
                overrides.setdefault("add", {})
                overrides.setdefault("remove", [])
                if action == "remove":
                    if alias not in configured_file_roots():
                        raise ValueError("找不到这个目录别名")
                    overrides["add"].pop(alias, None)
                    if alias not in yaml.safe_load((ROOT / "config" / "file_roots.yaml").read_text(encoding="utf-8")).get("roots", {}):
                        overrides["remove"] = list(set(overrides["remove"] + [alias]))
                    else:
                        overrides["remove"] = list(set(overrides["remove"] + [alias]))
                elif action == "add":
                    path = Path(str(body.get("path", ""))).expanduser().resolve(strict=True)
                    if not path.is_dir():
                        raise ValueError("选择的路径不是一个文件夹")
                    with os.scandir(path) as entries:
                        next(entries, None)
                    if alias in configured_file_roots():
                        raise ValueError("这个目录别名已存在")
                    overrides["add"][alias] = str(path)
                else:
                    raise ValueError("未知目录操作")
                save_json(override_path, overrides)
                self._json(200, {"ok": True, "roots": file_root_status()})
                return

            if route in {"/api/files/send", "/api/files/zip"}:
                selections = body.get("paths", []) if route.endswith("/zip") else [body.get("path", "")]
                if not isinstance(selections, list) or not selections or len(selections) > 30:
                    raise ValueError("请选 1–30 个文件或文件夹")
                temp_archive = None
                try:
                    if route.endswith("/zip"):
                        temp_archive = create_archive([str(item) for item in selections])
                        transfer_path = temp_archive
                        comment = "来自本机 harness 的选定文件/文件夹压缩包。"
                    else:
                        _, _, transfer_path = resolve_shared_path(str(selections[0]))
                        if not transfer_path.is_file():
                            raise ValueError("请选择单个文件；文件夹请使用打包回传")
                        if is_sensitive_transfer_path(transfer_path):
                            raise ValueError("出于安全考虑，.env、密钥和证书文件不能通过 Slack 回传")
                        upload_limit = file_transfer_limits()[0]
                        if transfer_path.stat().st_size > upload_limit:
                            raise ValueError(f"文件超过配置的 {upload_limit // (1024 * 1024)} MB 上限")
                        comment = "来自本机 harness 的文件回传。"
                    self._send_to_slack(transfer_path, comment)
                    save_record("slack_file_upload", {"kind": "zip" if route.endswith("/zip") else "single", "bytes": transfer_path.stat().st_size})
                    self._json(200, {"ok": True, "name": transfer_path.name, "bytes": transfer_path.stat().st_size})
                finally:
                    if temp_archive:
                        temp_archive.unlink(missing_ok=True)
                return

            if route == "/api/cloud/log":
                cost = float(body.get("cost_usd", -1))
                duration = float(body.get("duration_minutes", 0))
                if not math.isfinite(cost) or not math.isfinite(duration) or cost < 0 or duration < 0:
                    raise ValueError("费用与时长必须是非负有效数字")
                provider = str(body.get("provider", "runpod"))[:32]
                task = str(body.get("task", "GPU 任务"))[:120]
                policy = yaml.safe_load((ROOT / "config" / "cloud_policy.yaml").read_text(encoding="utf-8")) or {}
                cloud_override_path = ROOT / "data" / "cloud_policy_override.json"
                if cloud_override_path.is_file():
                    policy.update(json.loads(cloud_override_path.read_text(encoding="utf-8")))
                limit = float(policy.get("monthly_budget_usd", 10))
                data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
                if not data_dir.is_absolute():
                    data_dir = ROOT / data_dir
                month_key = datetime.now().astimezone().strftime("%Y-%m")
                spent = 0.0
                for row in read_jsonl(data_dir / "cloud_jobs.jsonl"):
                    try:
                        row_month = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).astimezone().strftime("%Y-%m")
                    except (KeyError, ValueError):
                        continue
                    if row_month == month_key:
                        spent += float(row.get("cost_usd", 0))
                per_job_cap = float(policy.get("per_job_budget_usd", 2))
                if cost > per_job_cap:
                    raise ValueError(f"这笔费用超过单任务预算上限 ${per_job_cap:.2f}")
                if spent + cost > limit:
                    raise ValueError(f"记录后将超过本月预算上限 ${limit:.2f}；当前累计 ${spent:.2f}")
                save_record("cloud_job", {"provider": provider, "task": task, "cost_usd": round(cost, 4), "duration_minutes": round(duration, 2)})
                ledger_path = data_dir / "cloud_jobs.jsonl"
                with ledger_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "provider": provider, "task": task, "cost_usd": round(cost, 4), "duration_minutes": round(duration, 2)}, ensure_ascii=False) + "\n")
                self._json(200, {"ok": True, "spent_month_usd": round(spent + cost, 4), "budget_usd": limit})
                return

            if route == "/api/cloud/policy":
                base = yaml.safe_load((ROOT / "config" / "cloud_policy.yaml").read_text(encoding="utf-8")) or {}
                values = {}
                for key, upper in (("monthly_budget_usd", 10.0), ("per_job_budget_usd", 2.0), ("max_gpu_hourly_usd", 1.25)):
                    value = float(body.get(key, base[key]))
                    if not math.isfinite(value) or value <= 0 or value > upper:
                        raise ValueError(f"{key} 必须在 0 到 ${upper:.2f} 之间（包含原定预算上限）")
                    values[key] = round(value, 2)
                data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
                if not data_dir.is_absolute():
                    data_dir = ROOT / data_dir
                save_json(data_dir / "cloud_policy_override.json", values)
                self._json(200, {"ok": True, "policy": values})
                return

            raise ValueError("未知操作")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, "text/html; charset=utf-8", html_path.read_bytes())
            elif parsed.path == "/api/dashboard":
                try:
                    self._json(200, dashboard_snapshot())
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
            elif parsed.path == "/api/files":
                query = parse_qs(parsed.query)
                location = query.get("path", [""])[0]
                try:
                    if not location:
                        self._json(200, {"roots": file_root_status()})
                        return
                    alias, _, path = resolve_shared_path(location)
                    if not path.is_dir():
                        raise ValueError("路径不是文件夹")
                    items = []
                    for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        if is_sensitive_transfer_path(item):
                            continue
                        relative = item.relative_to(configured_file_roots()[alias]).as_posix()
                        items.append({"name": item.name, "path": f"{alias}:{relative}", "directory": item.is_dir(), "bytes": item.stat().st_size if item.is_file() else 0})
                        if len(items) >= 250:
                            break
                    self._json(200, {"alias": alias, "location": location, "items": items})
                except PermissionError:
                    self._json(403, {"error": "macOS 拒绝访问此目录。请给运行本服务的终端应用授予该目录的‘文件与文件夹’权限，然后重启服务。"})
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            else:
                self._send(404, "text/plain; charset=utf-8", "未找到".encode())

        def do_POST(self) -> None:
            try:
                self._handle_api_post(urlparse(self.path).path)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except PermissionError:
                self._json(403, {"error": "macOS 拒绝访问本机目录，请授予运行服务的终端应用文件夹权限。"})
            except Exception:
                console.print_exception()
                self._json(502, {"error": "操作失败。请检查本机模型服务、Slack 权限或文件目录访问权限。"})

        def log_message(self, format: str, *args: Any) -> None:
            console.print(f"[dim]Web {self.address_string()} · {format % args}[/dim]")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise typer.BadParameter(f"网页端口 {port} 无法监听：{exc}") from exc
    console.print(f"本机日常对话网页：[link=http://127.0.0.1:{port}]http://127.0.0.1:{port}[/link]")
    console.print("仅监听 127.0.0.1；关闭此终端可停止网页服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("正在关闭网页服务。")
    finally:
        server.server_close()


if __name__ == "__main__":
    app()
