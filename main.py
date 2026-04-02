import asyncio
import base64
import json
import os
import re
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools, register



REQUEST_TIMEOUT = 10
POLL_INTERVAL = 5
COMPLETION_COOLDOWN_SEC = 30
TOKEN_REFRESH_LEAD_SEC = 60
TOKEN_REFRESH_FALLBACK_SEC = 15 * 60



def _now() -> float:
    return time.time()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_cpu_label(value: str) -> str:
    return re.sub(r"[\s#]", "", _normalize_text(value).lower())


def _decode_jwt_exp(token: str) -> int:
    if not token or "." not in token:
        return 0
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        body = base64.urlsafe_b64decode(payload.encode("utf-8"))
        parsed = json.loads(body.decode("utf-8"))
        exp = int(parsed.get("exp", 0))
        return exp if exp > 0 else 0
    except Exception:
        return 0


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_timestamp(ts: datetime | None = None) -> str:
    current = ts or datetime.now()
    return current.strftime("%m-%d %H:%M:%S")


def _encode_path_segment(value: str) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("?", "%3F")
        .replace("#", "%23")
    )


class StateStore:
    def __init__(self):
        self.data_dir = StarTools.get_data_dir("AppliedWebTerminalAstrbot")
        self.file_path = Path(self.data_dir) / "state.json"
        self.state = {"terminals": {}, "bindings": {}}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not self.file_path.exists():
            await self.save()
            return

        def _read():
            return json.loads(self.file_path.read_text(encoding="utf-8"))

        try:
            loaded = await asyncio.to_thread(_read)
            if isinstance(loaded, dict):
                self.state = {
                    "terminals": loaded.get("terminals", {}),
                    "bindings": loaded.get("bindings", {}),
                }
        except Exception as exc:
            logger.warning(f"[AE2] state.json 读取失败，已回退空状态: {exc}")
            self.state = {"terminals": {}, "bindings": {}}
            await self.save()

    async def save(self) -> None:
        async with self._lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.state, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self.file_path.write_text, payload, "utf-8")

    def list_terminal_configs(self) -> list[dict]:
        out = []
        for uuid, item in self.state["terminals"].items():
            out.append(
                {
                    "uuid": uuid,
                    "name": _normalize_text(item.get("name")) or uuid,
                    "password": _normalize_text(item.get("password")),
                }
            )
        return out

    def list_group_bindings(self, session: str) -> list[dict]:
        subs = self.state["bindings"].get(session, {}).get("terminalSubs", {})
        out = []
        for uuid, sub in subs.items():
            terminal = self.state["terminals"].get(uuid, {})
            out.append(
                {
                    "uuid": uuid,
                    "name": _normalize_text(terminal.get("name")) or uuid,
                    "password": _normalize_text(terminal.get("password")),
                    "watchAll": bool(sub.get("watchAll", True)),
                    "cpuIds": [str(x) for x in sub.get("cpuIds", [])],
                }
            )
        return out

    def get_group_binding(self, session: str, uuid: str) -> dict | None:
        for item in self.list_group_bindings(session):
            if item["uuid"] == uuid:
                return item
        return None

    async def bind_terminal(
        self,
        session: str,
        *,
        uuid: str,
        name: str,
        password: str,
        watch_all: bool = True,
        cpu_ids: list[str] | None = None,
    ) -> None:
        self.state["terminals"][uuid] = {"name": name, "password": password}
        self.state["bindings"].setdefault(session, {"terminalSubs": {}})
        ids = sorted({str(x) for x in (cpu_ids or [])}, key=lambda x: int(x))
        self.state["bindings"][session]["terminalSubs"][uuid] = {
            "watchAll": watch_all,
            "cpuIds": ids,
        }
        await self.save()

    async def unbind_terminal(self, session: str, uuid: str) -> bool:
        group_state = self.state["bindings"].get(session, {})
        subs = group_state.get("terminalSubs", {})
        if uuid not in subs:
            return False
        del subs[uuid]
        if not subs:
            self.state["bindings"].pop(session, None)
        self._gc_terminal(uuid)
        await self.save()
        return True

    async def update_binding_subscription(
        self,
        session: str,
        uuid: str,
        *,
        watch_all: bool,
        cpu_ids: list[str],
    ) -> bool:
        group_state = self.state["bindings"].get(session, {})
        sub = group_state.get("terminalSubs", {}).get(uuid)
        if not sub:
            return False
        sub["watchAll"] = bool(watch_all)
        sub["cpuIds"] = sorted({str(x) for x in cpu_ids}, key=lambda x: int(x))
        await self.save()
        return True

    def find_cpu_subscribed_sessions(self, terminal_uuid: str, cpu_id: str) -> list[str]:
        targets = []
        for session, group_state in self.state["bindings"].items():
            sub = group_state.get("terminalSubs", {}).get(terminal_uuid)
            if not sub:
                continue
            if sub.get("watchAll"):
                targets.append(session)
                continue
            cpu_ids = {str(x) for x in sub.get("cpuIds", [])}
            if cpu_id in cpu_ids:
                targets.append(session)
        return targets

    def find_watch_all_sessions(self, terminal_uuid: str) -> list[str]:
        targets = []
        for session, group_state in self.state["bindings"].items():
            sub = group_state.get("terminalSubs", {}).get(terminal_uuid)
            if sub and sub.get("watchAll"):
                targets.append(session)
        return targets

    def is_terminal_referenced(self, uuid: str) -> bool:
        for group_state in self.state["bindings"].values():
            if uuid in group_state.get("terminalSubs", {}):
                return True
        return False

    def _gc_terminal(self, uuid: str) -> None:
        if not self.is_terminal_referenced(uuid):
            self.state["terminals"].pop(uuid, None)


class TranslationService:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.language = "zh_cn"
        self.cache: dict[str, str] = {}

    async def render_item_name(self, crafting_status: dict | None) -> str:
        if not crafting_status:
            return "无任务"
        localized = await self.render_component(crafting_status.get("displayName"))
        if localized:
            return localized
        fallback = _normalize_text(crafting_status.get("itemName"))
        if fallback:
            return fallback
        fallback = _normalize_text(crafting_status.get("itemId"))
        return fallback or "无任务"

    async def render_component(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if text.startswith("{") or text.startswith("["):
                try:
                    return await self.render_component(json.loads(text))
                except Exception:
                    return text
            return text
        if isinstance(value, list):
            parts = [await self.render_component(item) for item in value]
            return "".join(parts).strip()
        if not isinstance(value, dict):
            return str(value).strip()

        text = ""
        if isinstance(value.get("text"), str):
            text += value["text"]
        elif isinstance(value.get("translate"), str):
            key = value["translate"]
            template = await self.translate_key(key)
            args = []
            raw_args = value.get("with")
            if isinstance(raw_args, list):
                for item in raw_args:
                    args.append(await self.render_component(item))
            text += self._format_translation(template, args)

        extra = value.get("extra")
        if isinstance(extra, list):
            parts = [await self.render_component(item) for item in extra]
            text += "".join(parts)
        return text.strip()

    async def translate_key(self, key: str) -> str:
        if key in self.cache:
            return self.cache[key]

        def _fetch() -> str:
            encoded = "/".join(requests.utils.quote(part, safe="") for part in key.split("/"))
            resp = requests.get(
                f"{self.base_url}/translate/{self.language}/{encoded}",
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"translate http {resp.status_code}")
            return resp.text

        try:
            translated = await asyncio.to_thread(_fetch)
        except Exception:
            translated = key
        self.cache[key] = translated
        return translated

    @staticmethod
    def _format_translation(template: str, args: list[str]) -> str:
        if not args:
            return template
        text = str(template)
        seq = 0
        while "%s" in text and seq < len(args):
            text = text.replace("%s", args[seq], 1)
            seq += 1
        for idx, arg in enumerate(args, start=1):
            text = text.replace(f"%{idx}$s", arg)
        return text


class ItemIconService:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cache: dict[str, bytes | None] = {}

    async def get_icon_bytes(self, item_type: str, item_id: str) -> bytes | None:
        t = _normalize_text(item_type)
        i = _normalize_text(item_id)
        if not t or not i:
            return None
        key = f"{t}|{i}"
        if key in self.cache:
            return self.cache[key]

        def _fetch() -> bytes | None:
            url = f"{self.base_url}/aeResource/{_encode_path_segment(t)}/{_encode_path_segment(i)}"
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return None
            content_type = _normalize_text(resp.headers.get("content-type")).lower()
            if "image" not in content_type:
                return None
            return resp.content if resp.content else None

        data = await asyncio.to_thread(_fetch)
        self.cache[key] = data
        return data


STATUS_TMPL = '''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #08101e; font-family: "PingFang SC", "Microsoft YaHei", Arial, sans-serif; color: #e8f4ff; padding: 24px; min-width: 800px; }
  .header { background: #122e4c; border-radius: 18px; padding: 20px 24px; margin-bottom: 18px; }
  .header h1 { font-size: 28px; color: #e8f4ff; margin-bottom: 12px; }
  .header .time { font-size: 14px; color: #a0c4e8; margin-bottom: 8px; }
  .header .summary { font-size: 20px; color: #d0e6f8; }
  .terminal { background: #0a1626; border-radius: 16px; padding: 16px; margin-bottom: 18px; border: 1px solid #375c84; }
  .terminal-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
  .terminal-name { font-size: 20px; color: #eef6ff; }
  .terminal-uuid { font-size: 14px; color: #8fa6c4; margin-top: 4px; }
  .terminal-status { font-size: 14px; color: #b9d3ee; }
  .cpu-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .cpu-card { border-radius: 12px; padding: 12px; }
  .cpu-card.busy { background: #361c12; border: 1px solid #d06234; }
  .cpu-card.idle { background: #101e30; border: 1px solid #487098; }
  .cpu-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .cpu-title { font-size: 22px; color: #f2f7ff; }
  .cpu-subtitle { font-size: 16px; color: #9eb6ce; margin-top: 2px; }
  .state-badge { padding: 2px 10px; border-radius: 10px; font-size: 14px; }
  .state-badge.busy { background: #ec742a; color: #fff7ec; }
  .state-badge.idle { background: #204c6e; color: #e8f4ff; }
  .cpu-task { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
  .cpu-icon { width: 44px; height: 44px; background: #2c3c54; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; color: #b4c4dc; flex-shrink: 0; }
  .cpu-icon img { width: 36px; height: 36px; }
  .cpu-task-info { flex: 1; }
  .task-name { font-size: 16px; color: #ffecd4; }
  .cpu-meta { display: flex; justify-content: space-between; margin-top: 12px; font-size: 16px; color: #f4dec4; }
  .idle-text { font-size: 16px; color: #c0d4e8; margin-top: 20px; }
  .no-cpus { background: #122034; border-radius: 12px; padding: 16px; text-align: center; color: #c2d4e8; font-size: 16px; }
</style>
</head>
<body>
  <div class="header">
    <h1>{{ title }}</h1>
    <div class="time">生成时间 {{ timestamp }}</div>
    <div class="summary">{{ terminal_count }} 个终端  |  {{ total_cpu }} 个 CPU  |  {{ total_busy }} 个忙碌中</div>
  </div>
  {% for terminal in terminals %}
  <div class="terminal">
    <div class="terminal-header">
      <div>
        <div class="terminal-name">{{ terminal.name }}</div>
        <div class="terminal-uuid">{{ terminal.uuid }}</div>
      </div>
      <div class="terminal-status">{{ terminal.connectText }}  |  忙碌 {{ terminal.busyCount }}/{{ terminal.cpuCount }}</div>
    </div>
    {% if terminal.cpus %}
    <div class="cpu-grid">
      {% for cpu in terminal.cpus %}
      <div class="cpu-card {{ cpu.stateClass }}">
        <div class="cpu-header">
          <div>
            <div class="cpu-title">CPU #{{ cpu.id }}</div>
            {% if cpu.showSubtitle %}
            <div class="cpu-subtitle">{{ cpu.name }}</div>
            {% endif %}
          </div>
          <span class="state-badge {{ cpu.stateClass }}">{{ cpu.stateText }}</span>
        </div>
        {% if cpu.busy %}
        <div class="cpu-task">
          <div class="cpu-icon">
            {% if cpu.icon_url %}<img src="{{ cpu.icon_url }}" alt="icon">{% else %}?{% endif %}
          </div>
          <div class="cpu-task-info">
            <div class="task-name">{{ cpu.taskName }}</div>
          </div>
        </div>
        <div class="cpu-meta">
          <span>x{{ cpu.amount }}</span>
          <span>{{ cpu.durationText }}</span>
        </div>
        {% else %}
        <div class="idle-text">当前无合成任务</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="no-cpus">暂无 CPU 状态</div>
    {% endif %}
  </div>
  {% endfor %}
</body>
</html>
'''

COMPLETION_TMPL = '''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a1422; font-family: "PingFang SC", "Microsoft YaHei", Arial, sans-serif; color: #e8f4ff; padding: 24px; min-width: 600px; }
  .header { border-radius: 16px; padding: 16px 20px; margin-bottom: 16px; }
  .header.cpu-done { background: #144636; }
  .header.terminal-done { background: #143656; }
  .header h1 { font-size: 28px; color: #f2faff; margin-bottom: 10px; }
  .header .info { font-size: 14px; color: #c0dcf6; margin-bottom: 4px; }
  .header .time { font-size: 14px; color: #a4c6e8; }
  .body-card { background: #0e1c2e; border-radius: 16px; padding: 16px; border: 1px solid #486e96; }
  .cpu-title { font-size: 20px; color: #eef6ff; margin-bottom: 6px; }
  .cpu-subtitle { font-size: 16px; color: #a2bac4; margin-bottom: 12px; }
  .task-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .task-icon { width: 56px; height: 56px; background: #2c3c54; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 24px; color: #b4c4dc; flex-shrink: 0; }
  .task-icon img { width: 48px; height: 48px; }
  .task-name { font-size: 18px; color: #ffecd4; }
  .meta-row { display: flex; gap: 24px; margin-top: 16px; font-size: 16px; color: #eedcc2; }
  .task-list-item { display: flex; align-items: center; gap: 8px; background: #162840; border-radius: 10px; padding: 8px 12px; margin-bottom: 8px; }
  .task-list-item .mini-icon { width: 22px; height: 22px; background: #2c3c54; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 12px; color: #b4c4dc; flex-shrink: 0; }
  .task-list-item .mini-icon img { width: 18px; height: 18px; }
  .task-list-item .name { font-size: 14px; color: #e4eefc; }
  .empty { font-size: 16px; color: #b0c6de; }
</style>
</head>
<body>
  <div class="header {{ headerClass }}">
    <h1>{{ title }}</h1>
    <div class="info">终端: {{ terminalName }} | {{ terminalUuid }}</div>
    <div class="time">时间: {{ timestamp }}</div>
  </div>
  <div class="body-card">
    {% if isCpuCompleted %}
    <div class="cpu-title">CPU #{{ cpuId }}</div>
    {% if showCpuSubtitle %}
    <div class="cpu-subtitle">{{ cpuName }}</div>
    {% endif %}
    <div class="task-row">
      <div class="task-icon">
        {% if icon_url %}<img src="{{ icon_url }}" alt="icon">{% else %}?{% endif %}
      </div>
      <div class="task-name">{{ taskName }}</div>
    </div>
    <div class="meta-row">
      <span>数量 x{{ amount }}</span>
      <span>耗时 {{ durationText }}</span>
      <span>状态 已完成</span>
    </div>
    {% else %}
    <div class="cpu-title">本轮占用 CPU: {{ cpuCount }}</div>
    {% if tasks %}
    {% for task in tasks %}
    <div class="task-list-item">
      <div class="mini-icon">
        {% if task.icon_url %}<img src="{{ task.icon_url }}" alt="icon">{% else %}?{% endif %}
      </div>
      <div class="name">{{ task.index }}. {{ task.name }}</div>
    </div>
    {% endfor %}
    {% else %}
    <div class="empty">无任务摘要</div>
    {% endif %}
    {% endif %}
  </div>
</body>
</html>
'''


class AppliedWebTerminalClient:
    def __init__(
        self,
        *,
        base_url: str,
        terminal_uuid: str,
        terminal_name: str,
        password: str,
        on_cpu_completed,
        on_terminal_completed,
    ):
        self.base_url = base_url
        self.terminal_uuid = terminal_uuid
        self.terminal_name = terminal_name
        self.password = password
        self.on_cpu_completed = on_cpu_completed
        self.on_terminal_completed = on_terminal_completed

        self._token = ""
        self._token_exp = 0
        self._connected = False
        self._running = False
        self._task: asyncio.Task | None = None
        self._snapshot: dict[str, dict] = {}
        self._last_cpu_completion_at: dict[str, float] = {}
        self._last_terminal_completion_at = 0.0

    def view(self) -> dict:
        cpus = [deepcopy(v) for _, v in sorted(self._snapshot.items(), key=lambda x: int(x[0]))]
        return {
            "uuid": self.terminal_uuid,
            "name": self.terminal_name,
            "connected": self._connected,
            "snapshot": cpus,
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                cpus = await self._fetch_cpus()
                self._connected = True
                self._apply_snapshot(cpus)
            except Exception as exc:
                self._connected = False
                logger.warning(
                    f"[AE2] 轮询失败 {self.terminal_name}({self.terminal_uuid}): {exc}"
                )
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_cpus(self) -> list[dict]:
        data = await self._fetch_json("/crafting/cpus")
        if not isinstance(data, list):
            return []
        return [self._normalize_cpu(cpu) for cpu in data]

    async def _fetch_json(self, route: str, retry: bool = True) -> Any:
        await self._ensure_token()

        def _request(token: str):
            return requests.get(
                f"{self.base_url}{route}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )

        resp = await asyncio.to_thread(_request, self._token)
        if resp.status_code == 401 and retry:
            await self._refresh_token(force=True)
            return await self._fetch_json(route, retry=False)
        if resp.status_code != 200:
            raise RuntimeError(f"请求失败 {route}: HTTP {resp.status_code}")
        return resp.json()

    async def _ensure_token(self) -> None:
        refresh_at = self._token_exp - TOKEN_REFRESH_LEAD_SEC
        if not self._token or _now() >= refresh_at:
            await self._refresh_token(force=True)

    async def _refresh_token(self, force: bool = False) -> None:
        if not force and self._token and _now() < self._token_exp - TOKEN_REFRESH_LEAD_SEC:
            return

        def _login():
            return requests.post(
                f"{self.base_url}/login",
                json={"uuid": self.terminal_uuid, "password": self.password},
                timeout=REQUEST_TIMEOUT,
            )

        resp = await asyncio.to_thread(_login)
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败 HTTP {resp.status_code}")
        body = resp.json()
        if not body.get("success") or not body.get("payload"):
            raise RuntimeError(f"登录失败: {body.get('message') or body.get('payload') or '未知错误'}")

        token = str(body["payload"])
        exp = _decode_jwt_exp(token)
        if exp <= 0:
            exp = int(_now()) + TOKEN_REFRESH_FALLBACK_SEC
        self._token = token
        self._token_exp = exp

    def _apply_snapshot(self, cpus: list[dict]) -> None:
        prev = self._snapshot
        nxt: dict[str, dict] = {}
        for cpu in cpus:
            cpu_id = str(cpu["id"])
            nxt[cpu_id] = cpu
            prev_cpu = prev.get(cpu_id)
            if prev_cpu and prev_cpu.get("busy") and not cpu.get("busy") and self._allow_cpu_completed(cpu_id):
                event = {
                    "terminalUuid": self.terminal_uuid,
                    "terminalName": self.terminal_name,
                    "cpuId": cpu_id,
                    "previous": deepcopy(prev_cpu),
                    "current": deepcopy(cpu),
                }
                asyncio.create_task(self.on_cpu_completed(event))

        prev_busy = [cpu for cpu in prev.values() if cpu.get("busy")]
        nxt_busy = [cpu for cpu in nxt.values() if cpu.get("busy")]
        if prev_busy and not nxt_busy and self._allow_terminal_completed():
            event = {
                "terminalUuid": self.terminal_uuid,
                "terminalName": self.terminal_name,
                "previousBusy": deepcopy(prev_busy),
            }
            asyncio.create_task(self.on_terminal_completed(event))

        self._snapshot = nxt

    def _allow_cpu_completed(self, cpu_id: str) -> bool:
        now = _now()
        last = self._last_cpu_completion_at.get(cpu_id, 0)
        if now - last < COMPLETION_COOLDOWN_SEC:
            return False
        self._last_cpu_completion_at[cpu_id] = now
        return True

    def _allow_terminal_completed(self) -> bool:
        now = _now()
        if now - self._last_terminal_completion_at < COMPLETION_COOLDOWN_SEC:
            return False
        self._last_terminal_completion_at = now
        return True

    @staticmethod
    def _normalize_cpu(cpu: dict) -> dict:
        cpu_id = str(cpu.get("id", "0"))
        cpu_name = _normalize_text(cpu.get("name")) or f"CPU #{cpu_id}"
        status = AppliedWebTerminalClient._normalize_crafting_status(cpu.get("craftingStatus"))
        return {
            "id": cpu_id,
            "name": cpu_name,
            "busy": bool(cpu.get("busy")),
            "craftingStatus": status,
        }

    @staticmethod
    def _normalize_crafting_status(status: dict | None) -> dict | None:
        if not status or not isinstance(status, dict):
            return None
        crafting = status.get("crafting") if isinstance(status.get("crafting"), dict) else {}
        what = crafting.get("what") if isinstance(crafting.get("what"), dict) else {}
        return {
            "itemName": _normalize_text(what.get("displayName")) or _normalize_text(what.get("id")),
            "itemId": _normalize_text(what.get("id")),
            "itemType": _normalize_text(what.get("type")),
            "displayName": what.get("displayName"),
            "amount": int(crafting.get("amount") or 0),
            "elapsedTimeNanos": int(status.get("elapsedTimeNanos") or 0),
        }


@register("AppliedWebTerminalAstrbot", "icewindy", "AE2WebTerminal QQ 群监控插件", "0.2.0")
class AppliedWebTerminalAstrbot(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self._get_base_url()
        self.mute_patterns: list[re.Pattern] = self._load_mute_keywords()
        self.enable_terminal_report = bool(self.config.get("enable_terminal_report", True))
        self.state = StateStore()
        self.translation = TranslationService(self.base_url)
        self.icon_service = ItemIconService(self.base_url)
        self.clients: dict[str, AppliedWebTerminalClient] = {}
        self._clients_lock = asyncio.Lock()
        self.data_dir = Path(StarTools.get_data_dir("AppliedWebTerminalAstrbot"))

    def _get_base_url(self) -> str:
        url = str(self.config.get("base_url", "")).strip()
        if url:
            return url.rstrip("/")
        env_url = os.getenv("AWT_BASE_URL")
        if env_url:
            return env_url.rstrip("/")
        logger.warning("[AE2] 未配置 base_url，请在插件设置中填写 AE2 Web Terminal 地址。功能将不可用，直到配置完成。")
        return ""

    def _load_mute_keywords(self) -> list[re.Pattern]:
        keywords = self.config.get("mute_keywords", [])
        patterns = []
        for kw in keywords:
            try:
                patterns.append(re.compile(kw))
            except re.error as exc:
                logger.warning(f"[AE2] 无效的正则表达式 '{kw}': {exc}")
        return patterns

    async def initialize(self):
        if not self.base_url:
            logger.warning("[AE2] base_url 未配置，跳过终端初始化。请在插件设置中配置。")
            return
        await self.state.load()
        for terminal in self.state.list_terminal_configs():
            try:
                await self._ensure_terminal(
                    terminal["uuid"],
                    terminal["name"],
                    terminal["password"],
                )
            except Exception as exc:
                logger.warning(f"[AE2] 初始化终端失败 {terminal['uuid']}: {exc}")

    async def terminate(self):
        for client in list(self.clients.values()):
            await client.stop()
        self.clients.clear()

    @filter.command("ae")
    async def ae_command(self, event: AstrMessageEvent):
        """AE2 监控命令。"""
        if not event.get_group_id():
            yield event.plain_result("[AE2] 请在群聊中使用该命令。")
            return

        text = _normalize_text(event.get_message_str())
        parts = [p for p in text.split(" ") if p]
        action = parts[1].lower() if len(parts) > 1 else "help"

        try:
            if action in {"help", "帮助"}:
                yield event.plain_result(self._help_text())
                return

            if action in {"terminals", "终端"}:
                yield event.plain_result(await self._render_available_terminals())
                return

            if action in {"list", "列表", "binds", "绑定"}:
                yield event.plain_result(self._render_bindings(event.unified_msg_origin))
                return

            if action in {"bind", "绑定终端"}:
                if len(parts) < 4:
                    yield event.plain_result("[AE2] 用法: /ae bind <终端UUID> <密码>")
                    return
                msg = await self._handle_bind(event.unified_msg_origin, parts[2], parts[3])
                yield event.plain_result(msg)
                return

            if action in {"unbind", "解绑"}:
                if len(parts) < 3:
                    yield event.plain_result("[AE2] 用法: /ae unbind <终端UUID>")
                    return
                msg = await self._handle_unbind(event.unified_msg_origin, parts[2])
                yield event.plain_result(msg)
                return

            if action in {"statusimg", "statusimage", "图片状态", "状态图"}:
                uuid = parts[2] if len(parts) >= 3 else None
                result = await self._reply_status(event, event.unified_msg_origin, uuid, fmt="image", busy_only=False)
                yield result
                return

            if action in {"status", "状态"}:
                uuid, fmt, ok, busy_only = self._parse_status_args(parts[2:])
                if not ok:
                    yield event.plain_result("[AE2] 用法: /ae status [终端UUID] [image|text] [busy]")
                    return
                result = await self._reply_status(event, event.unified_msg_origin, uuid, fmt, busy_only)
                yield result
                return

            if action in {"watch", "订阅"}:
                msg = await self._handle_watch(event.unified_msg_origin, parts[2:], watch=True)
                yield event.plain_result(msg)
                return

            if action in {"unwatch", "取消"}:
                msg = await self._handle_watch(event.unified_msg_origin, parts[2:], watch=False)
                yield event.plain_result(msg)
                return

            yield event.plain_result(self._help_text())
        except Exception as exc:
            logger.exception(f"[AE2] 指令处理失败: {exc}")
            yield event.plain_result(f"[AE2] 处理失败: {exc}")

    def _help_text(self) -> str:
        return "\n".join(
            [
                "[AE2] 可用命令:",
                "/ae terminals",
                "/ae bind <终端UUID> <密码>",
                "/ae unbind <终端UUID>",
                "/ae list",
                "/ae status",
                "/ae status text",
                "/ae status image",
                "/ae status busy",
                "/ae status <终端UUID>",
                "/ae status <终端UUID> text",
                "/ae status <终端UUID> image",
                "/ae status <终端UUID> busy",
                "/ae statusimg [终端UUID]",
                "/ae watch <终端UUID> all",
                "/ae watch <终端UUID> cpu <编号>",
                "/ae unwatch <终端UUID> all",
                "/ae unwatch <终端UUID> cpu <编号>",
                "若当前群只绑定一个终端，watch/unwatch 可省略 <终端UUID>",
            ]
        )

    @staticmethod
    def _parse_status_args(args: list[str]) -> tuple[str | None, str, bool, bool]:
        fmt = "image"
        uuid = None
        busy_only = False
        for token in args:
            low = token.lower()
            if low in {"image", "img", "图片", "图"}:
                fmt = "image"
                continue
            if low in {"text", "txt", "文字"}:
                fmt = "text"
                continue
            if low in {"busy", "忙碌", "忙"}:
                busy_only = True
                continue
            if uuid is None:
                uuid = token
                continue
            return None, "image", False, False
        return uuid, fmt, True, busy_only

    async def _list_available_terminals(self) -> list[dict]:
        def _call():
            return requests.get(f"{self.base_url}/list", timeout=REQUEST_TIMEOUT)

        resp = await asyncio.to_thread(_call)
        if resp.status_code != 200:
            raise RuntimeError(f"读取终端列表失败: HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, list):
            return []

        out = []
        for item in data:
            uuid = _normalize_text(item.get("uuid"))
            if not uuid:
                continue
            out.append({"uuid": uuid, "name": _normalize_text(item.get("name")) or uuid})
        return out

    async def _render_available_terminals(self) -> str:
        terminals = await self._list_available_terminals()
        if not terminals:
            return "[AE2] 当前没有发现可绑定终端。"
        return "\n".join(["[AE2] 可绑定终端:"] + [f"{t['name']} | {t['uuid']}" for t in terminals])

    def _render_bindings(self, session: str) -> str:
        bindings = self.state.list_group_bindings(session)
        if not bindings:
            return "[AE2] 当前群没有绑定任何终端。"
        lines = ["[AE2] 当前群绑定终端:"]
        for b in bindings:
            view = self.clients[b["uuid"]].view() if b["uuid"] in self.clients else None
            connected = "已连接" if (view and view["connected"]) else "未连接"
            watch_text = "全部 CPU" if b["watchAll"] else ", ".join(f"CPU #{x}" for x in b["cpuIds"]) or "无"
            lines.append(f"{b['name']} | {b['uuid']}")
            lines.append(f"连接: {connected} | 订阅: {watch_text}")
        return "\n".join(lines)

    async def _handle_bind(self, session: str, uuid: str, password: str) -> str:
        terminals = await self._list_available_terminals()
        target = next((x for x in terminals if x["uuid"] == uuid), None)
        if not target:
            return f"[AE2] 找不到终端 {uuid}，先用 /ae terminals 查看可绑定终端。"

        await self._ensure_terminal(uuid, target["name"], password)
        await self.state.bind_terminal(
            session,
            uuid=uuid,
            name=target["name"],
            password=password,
            watch_all=True,
            cpu_ids=[],
        )
        return f"[AE2] 已绑定终端 {target['name']} ({uuid})，默认订阅全部 CPU 完成提醒。"

    async def _handle_unbind(self, session: str, uuid: str) -> str:
        binding = self.state.get_group_binding(session, uuid)
        if not binding:
            return f"[AE2] 当前群没有绑定终端 {uuid}。"
        await self.state.unbind_terminal(session, uuid)
        if not self.state.is_terminal_referenced(uuid):
            await self._release_terminal(uuid)
        return f"[AE2] 已解绑终端 {binding['name']} ({uuid})。"

    async def _handle_watch(self, session: str, args: list[str], watch: bool) -> str:
        if len(args) < 1:
            return "[AE2] 用法: /ae watch <终端UUID> all|cpu <编号>"

        bindings = self.state.list_group_bindings(session)
        if not bindings:
            return "[AE2] 当前群没有绑定任何终端。"

        uuid: str | None = None
        tail = args
        first = args[0].lower()
        if first in {"all", "全部", "cpu"}:
            if len(bindings) != 1:
                return "[AE2] 当前群绑定了多个终端，请显式指定终端 UUID。"
            uuid = bindings[0]["uuid"]
            tail = args
        else:
            uuid = args[0]
            tail = args[1:]

        if not tail:
            return "[AE2] 用法: /ae watch <终端UUID> all|cpu <编号>"

        binding = self.state.get_group_binding(session, uuid)
        if not binding:
            return f"[AE2] 当前群未绑定终端 {uuid}。"

        mode = tail[0].lower()
        if mode in {"all", "全部"}:
            if watch:
                await self.state.update_binding_subscription(session, uuid, watch_all=True, cpu_ids=[])
                return f"[AE2] 已将终端 {binding['name']} 设置为订阅全部 CPU。"
            await self.state.update_binding_subscription(session, uuid, watch_all=False, cpu_ids=[])
            return f"[AE2] 已清空终端 {binding['name']} 的订阅。"

        if mode == "cpu" and len(tail) >= 2 and tail[1].isdigit():
            cpu_id = str(int(tail[1]))
            ids = [str(x) for x in binding["cpuIds"]]
            if watch:
                ids.append(cpu_id)
                await self.state.update_binding_subscription(session, uuid, watch_all=False, cpu_ids=ids)
                return f"[AE2] 已订阅终端 {binding['name']} 的 CPU #{cpu_id}。"
            ids = [x for x in ids if x != cpu_id]
            await self.state.update_binding_subscription(session, uuid, watch_all=False, cpu_ids=ids)
            return f"[AE2] 已取消订阅终端 {binding['name']} 的 CPU #{cpu_id}。"

        return "[AE2] 用法: /ae watch <终端UUID> all|cpu <编号>"

    async def _reply_status(self, event: AstrMessageEvent, session: str, uuid: str | None, fmt: str, busy_only: bool = False) -> MessageChain:
        payload = await self._build_status_payload(session, uuid, busy_only)
        if isinstance(payload, str):
            return MessageChain().message(payload)

        if fmt == "text":
            return MessageChain().message(self._render_status_text(payload))

        try:
            render_data = await self._prepare_status_render_data(payload)
            url = await self.html_render(STATUS_TMPL, render_data)
            return event.image_result(url)
        except Exception as exc:
            logger.warning(f"[AE2] 状态图片渲染失败，回退文本: {exc}")
            return MessageChain().message(self._render_status_text(payload))

    async def _build_status_payload(self, session: str, uuid: str | None, busy_only: bool = False) -> dict | str:
        bindings = self.state.list_group_bindings(session)
        if not bindings:
            return "[AE2] 当前群没有绑定任何终端。先用 /ae terminals 查看，再用 /ae bind 绑定。"

        selected = bindings
        if uuid:
            selected = [b for b in bindings if b["uuid"] == uuid]
            if not selected:
                return f"[AE2] 当前群未绑定终端 {uuid}。"

        terminals = []
        total_cpu = 0
        total_busy = 0

        for binding in selected:
            view = self.clients.get(binding["uuid"]).view() if binding["uuid"] in self.clients else None
            if not view:
                terminals.append(
                    {
                        "name": binding["name"],
                        "uuid": binding["uuid"],
                        "connected": False,
                        "cpuCount": 0,
                        "busyCount": 0,
                        "cpus": [],
                    }
                )
                continue

            cpus = []
            for cpu in view["snapshot"]:
                if not cpu.get("busy") or not cpu.get("craftingStatus"):
                    if busy_only:
                        continue
                    cpus.append(
                        {
                            "id": cpu["id"],
                            "name": cpu["name"],
                            "busy": False,
                            "taskName": "",
                            "amount": 0,
                            "durationText": "",
                            "iconBytes": None,
                        }
                    )
                    continue

                task = cpu["craftingStatus"]
                task_name = await self.translation.render_item_name(task)
                icon_bytes = await self.icon_service.get_icon_bytes(task.get("itemType"), task.get("itemId"))
                elapsed = int(task.get("elapsedTimeNanos") or 0) / 1_000_000_000
                cpus.append(
                    {
                        "id": cpu["id"],
                        "name": cpu["name"],
                        "busy": True,
                        "taskName": task_name,
                        "amount": int(task.get("amount") or 0),
                        "durationText": _format_duration(elapsed),
                        "iconBytes": icon_bytes,
                    }
                )

            busy_count = sum(1 for c in cpus if c["busy"])
            total_cpu += len(cpus)
            total_busy += busy_count
            terminals.append(
                {
                    "name": binding["name"],
                    "uuid": binding["uuid"],
                    "connected": view["connected"],
                    "cpuCount": len(cpus),
                    "busyCount": busy_count,
                    "cpus": cpus,
                }
            )

        return {
            "title": "[AE2] 当前合成状态",
            "terminals": terminals,
            "totalCpu": total_cpu,
            "totalBusy": total_busy,
        }

    def _render_status_text(self, payload: dict) -> str:
        lines = [payload["title"]]
        for terminal in payload["terminals"]:
            lines.append(f"终端: {terminal['name']} | {terminal['uuid']}")
            lines.append(f"连接状态: {'已连接' if terminal['connected'] else '未连接'}")
            if not terminal["cpus"]:
                lines.append("暂无 CPU 状态。")
                continue
            for cpu in terminal["cpus"]:
                cpu_title = f"CPU #{cpu['id']} {cpu['name']}"
                if not cpu["busy"]:
                    lines.append(f"{cpu_title}: 空闲")
                    continue
                lines.append(f"{cpu_title}: 忙碌")
                lines.append(f"任务: {cpu['taskName']} x{cpu['amount']}")
                lines.append(f"耗时: {cpu['durationText']}")
        return "\n".join(lines)

    async def _ensure_terminal(self, uuid: str, name: str, password: str) -> None:
        async with self._clients_lock:
            current = self.clients.get(uuid)
            if current and current.password == password:
                current.terminal_name = name
                return
            if current:
                await current.stop()
                self.clients.pop(uuid, None)

            client = AppliedWebTerminalClient(
                base_url=self.base_url,
                terminal_uuid=uuid,
                terminal_name=name,
                password=password,
                on_cpu_completed=self._on_cpu_completed,
                on_terminal_completed=self._on_terminal_completed,
            )
            self.clients[uuid] = client
            await client.start()

    async def _release_terminal(self, uuid: str) -> None:
        async with self._clients_lock:
            client = self.clients.pop(uuid, None)
            if client:
                await client.stop()

    async def _on_cpu_completed(self, event_payload: dict) -> None:
        task = event_payload.get("previous", {}).get("craftingStatus") or {}
        task_name = await self.translation.render_item_name(task)
        if self._is_muted(task_name):
            return
        icon_bytes = await self.icon_service.get_icon_bytes(task.get("itemType"), task.get("itemId"))
        amount = int(task.get("amount") or 0)
        duration = int(task.get("elapsedTimeNanos") or 0) / 1_000_000_000

        payload = {
            "type": "cpu-completed",
            "title": "[AE2] 合成完成",
            "terminalName": event_payload["terminalName"],
            "terminalUuid": event_payload["terminalUuid"],
            "cpuId": event_payload["cpuId"],
            "cpuName": event_payload.get("previous", {}).get("name", ""),
            "taskName": task_name,
            "amount": amount,
            "durationText": _format_duration(duration),
            "iconBytes": icon_bytes,
        }
        fallback = "\n".join(
            [
                payload["title"],
                f"终端: {payload['terminalName']} | {payload['terminalUuid']}",
                f"CPU #{payload['cpuId']}",
                f"任务: {payload['taskName']} x{payload['amount']}",
                f"耗时: {payload['durationText']}",
            ]
        )

        targets = self.state.find_cpu_subscribed_sessions(payload["terminalUuid"], payload["cpuId"])
        if not targets:
            return
        await self._broadcast_completion(targets, payload, fallback)

    async def _on_terminal_completed(self, event_payload: dict) -> None:
        if not self.enable_terminal_report:
            return

        task_map: dict[str, bytes | None] = {}
        for cpu in event_payload.get("previousBusy", []):
            task = cpu.get("craftingStatus")
            name = await self.translation.render_item_name(task)
            if not name or name in task_map or self._is_muted(name):
                continue
            icon_bytes = await self.icon_service.get_icon_bytes(
                task.get("itemType") if isinstance(task, dict) else "",
                task.get("itemId") if isinstance(task, dict) else "",
            )
            task_map[name] = icon_bytes

        if not task_map:
            return

        payload = {
            "type": "terminal-completed",
            "title": "[AE2] 合成队列完成",
            "terminalName": event_payload["terminalName"],
            "terminalUuid": event_payload["terminalUuid"],
            "cpuCount": len(event_payload.get("previousBusy", [])),
            "tasks": [{"name": k, "iconBytes": v} for k, v in task_map.items()],
        }
        task_text = "、".join(list(task_map.keys())[:6]) if task_map else "无任务摘要"
        fallback = "\n".join(
            [
                payload["title"],
                f"终端: {payload['terminalName']} | {payload['terminalUuid']}",
                f"本轮占用 CPU: {payload['cpuCount']}",
                f"任务摘要: {task_text}",
            ]
        )

        targets = self.state.find_watch_all_sessions(payload["terminalUuid"])
        if not targets:
            return
        await self._broadcast_completion(targets, payload, fallback)

    async def _broadcast_completion(self, sessions: list[str], payload: dict, fallback_text: str) -> None:
        for session in sessions:
            try:
                render_data = await self._prepare_completion_render_data(payload)
                url = await self.html_render(COMPLETION_TMPL, render_data)
                await self.context.send_message(session, MessageChain([Image.fromURL(url)]))
            except Exception as exc:
                logger.warning(f"[AE2] 完成提醒图片发送失败，回退文本: {exc}")
                await self.context.send_message(session, MessageChain().message(fallback_text))

    @staticmethod
    def _icon_to_data_url(icon_bytes: bytes | None) -> str | None:
        if not icon_bytes:
            return None
        b64 = base64.b64encode(icon_bytes).decode("utf-8")
        return f"data:image/png;base64,{b64}"

    def _is_muted(self, task_name: str) -> bool:
        if not self.mute_patterns:
            return False
        for pattern in self.mute_patterns:
            if pattern.search(task_name):
                return True
        return False

    async def _prepare_status_render_data(self, payload: dict) -> dict:
        terminals = []
        for terminal in payload["terminals"]:
            cpus = []
            for cpu in terminal["cpus"]:
                cpu_label = f"CPU #{cpu['id']}"
                cpu_name = cpu.get("name", "")
                show_subtitle = bool(cpu_name and _normalize_cpu_label(cpu_name) != _normalize_cpu_label(cpu_label))
                cpus.append({
                    "id": cpu["id"],
                    "name": cpu_name,
                    "busy": cpu["busy"],
                    "taskName": cpu.get("taskName", ""),
                    "amount": cpu.get("amount", 0),
                    "durationText": cpu.get("durationText", ""),
                    "icon_url": self._icon_to_data_url(cpu.get("iconBytes")),
                    "stateClass": "busy" if cpu["busy"] else "idle",
                    "stateText": "忙碌" if cpu["busy"] else "空闲",
                    "showSubtitle": show_subtitle,
                })
            terminals.append({
                "name": terminal["name"],
                "uuid": terminal["uuid"],
                "connected": terminal["connected"],
                "cpuCount": terminal["cpuCount"],
                "busyCount": terminal["busyCount"],
                "connectText": "已连接" if terminal["connected"] else "未连接",
                "cpus": cpus,
            })
        return {
            "title": payload["title"],
            "timestamp": _format_timestamp(),
            "terminal_count": len(payload["terminals"]),
            "total_cpu": payload["totalCpu"],
            "total_busy": payload["totalBusy"],
            "terminals": terminals,
        }

    async def _prepare_completion_render_data(self, payload: dict) -> dict:
        is_cpu = payload["type"] == "cpu-completed"
        data = {
            "title": payload["title"],
            "terminalName": payload["terminalName"],
            "terminalUuid": payload["terminalUuid"],
            "timestamp": _format_timestamp(),
            "headerClass": "cpu-done" if is_cpu else "terminal-done",
            "isCpuCompleted": is_cpu,
        }
        if is_cpu:
            cpu_label = f"CPU #{payload['cpuId']}"
            cpu_name = payload.get("cpuName", "")
            data["cpuId"] = payload["cpuId"]
            data["cpuName"] = cpu_name
            data["showCpuSubtitle"] = bool(cpu_name and _normalize_cpu_label(cpu_name) != _normalize_cpu_label(cpu_label))
            data["taskName"] = payload["taskName"]
            data["amount"] = payload["amount"]
            data["durationText"] = payload["durationText"]
            data["icon_url"] = self._icon_to_data_url(payload.get("iconBytes"))
        else:
            data["cpuCount"] = payload["cpuCount"]
            data["tasks"] = [
                {"name": t["name"], "icon_url": self._icon_to_data_url(t.get("iconBytes")), "index": i}
                for i, t in enumerate(payload.get("tasks", [])[:5], start=1)
            ]
        return data
