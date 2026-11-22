from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apscheduler.triggers.interval import IntervalTrigger

from app.sdk.browser import launch_browser_context
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase


class MTeamKeepAlive(_PluginBase):
    """通过 MoviePilot 托管的浏览器保存 M-Team 登录态并定期访问站点。"""

    plugin_name = "馒头保活"
    plugin_desc = "人工完成馒头登录，保存浏览器登录态，并按周期访问站点。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.0"
    plugin_author = "AnonyQ"
    author_url = "https://github.com/AnonyQ"
    plugin_config_prefix = "mteamkeepalive_"
    plugin_order = 50
    auth_level = 1

    DEFAULT_SITE_URL = "https://kp.m-team.cc/"
    DEFAULT_INTERVAL_HOURS = 6
    DEFAULT_LOGIN_WAIT_MINUTES = 8
    MAX_INTERVAL_HOURS = 168
    MAX_LOGIN_WAIT_MINUTES = 20

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._site_url = self.DEFAULT_SITE_URL
        self._interval_hours = self.DEFAULT_INTERVAL_HOURS
        self._login_wait_minutes = self.DEFAULT_LOGIN_WAIT_MINUTES
        self._stopping = False
        self._run_lock = threading.Lock()
        self._status = "尚未初始化登录态。"

    # ---------------------------------------------------------------------
    # MoviePilot 生命周期
    # ---------------------------------------------------------------------
    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置。支持重复初始化，并把一次性操作交给 MP 调度器。"""
        config = dict(config or {})
        self._stopping = False
        self._enabled = bool(config.get("enabled", False))
        self._site_url = self._validate_site_url(
            str(config.get("site_url") or self.DEFAULT_SITE_URL).strip()
        )
        self._interval_hours = self._clamp_int(
            config.get("interval_hours", self.DEFAULT_INTERVAL_HOURS), 1, self.MAX_INTERVAL_HOURS,
            self.DEFAULT_INTERVAL_HOURS,
        )
        self._login_wait_minutes = self._clamp_int(
            config.get("login_wait_minutes", self.DEFAULT_LOGIN_WAIT_MINUTES),
            1, self.MAX_LOGIN_WAIT_MINUTES, self.DEFAULT_LOGIN_WAIT_MINUTES,
        )
        self._status = str(self.get_data("status") or "尚未初始化登录态。")

        # 勾选后保存配置，会触发一次人工登录窗口；标志位立即复位，避免反复打开。
        do_login = bool(config.get("login_once"))
        do_run_once = bool(config.get("onlyonce"))
        if do_login or do_run_once:
            config["login_once"] = False
            config["onlyonce"] = False
            self.update_config(config)

        if do_login:
            self._enqueue_once(
                "manual_login",
                self.open_login_browser,
                "打开馒头登录浏览器",
            )
        elif do_run_once:
            self._enqueue_once(
                "keepalive_once",
                self.keepalive,
                "立即执行一次馒头保活",
            )

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> list[dict[str, Any]]:
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用定时保活",
                            "hint": "启用后按设定周期访问馒头站点。",
                            "persistentHint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "site_url",
                            "label": "馒头站点地址",
                            "type": "url",
                            "placeholder": "https://kp.m-team.cc/",
                            "hint": "填写你平时登录馒头使用的完整站点地址。",
                            "persistentHint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "interval_hours",
                            "label": "保活间隔（小时）",
                            "type": "number",
                            "min": 1,
                            "max": self.MAX_INTERVAL_HOURS,
                            "hint": "建议先使用 6 小时；最小 1 小时。",
                            "persistentHint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "login_wait_minutes",
                            "label": "人工登录窗口时长（分钟）",
                            "type": "number",
                            "min": 1,
                            "max": self.MAX_LOGIN_WAIT_MINUTES,
                            "hint": "浏览器打开后，在此时间内手动完成邮箱验证码登录；登录完成后保持窗口打开，插件会自动保存状态。",
                            "persistentHint": True,
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "login_once",
                            "label": "打开人工登录窗口（保存后执行）",
                            "color": "primary",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "onlyonce",
                            "label": "立即执行一次保活（保存后执行）",
                            "color": "primary",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "site_url": self.DEFAULT_SITE_URL,
            "interval_hours": self.DEFAULT_INTERVAL_HOURS,
            "login_wait_minutes": self.DEFAULT_LOGIN_WAIT_MINUTES,
            "login_once": False,
            "onlyonce": False,
        }

    def get_page(self) -> list[dict]:
        """展示最近一次操作状态；不在页面中暴露 Cookie 或 Token。"""
        status = str(self.get_data("status") or self._status)
        last_run = str(self.get_data("last_run") or "暂无")
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "馒头保活状态",
                    "text": status,
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "secondary",
                    "variant": "tonal",
                    "text": f"最近操作时间：{last_run}",
                },
            },
        ]

    def get_service(self) -> list[dict]:
        """启用时注册宿主周期任务，不自行创建后台调度器。"""
        if not self._enabled:
            return []
        return [
            {
                "id": "MTeamKeepAlive.Refresh",
                "name": "馒头定时保活",
                "trigger": IntervalTrigger(hours=self._interval_hours),
                "func": self.keepalive,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停止插件服务。浏览器操作本身有超时，不会留下常驻线程。"""
        self._stopping = True
        self._enabled = False

    # ---------------------------------------------------------------------
    # 一次性任务与状态
    # ---------------------------------------------------------------------
    def _enqueue_once(self, task_id: str, func: Any, task_name: str) -> None:
        try:
            from app.sdk import scheduler as scheduler_sdk

            add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if not callable(add_once):
                self._set_status("当前 MoviePilot 未提供 add_plugin_once_job；请升级主程序后重试。")
                return
            accepted = add_once(
                self.__class__.__name__,
                task_id,
                func,
                task_name,
                delay_seconds=3,
            )
            if accepted is False:
                self._set_status(f"提交一次性任务失败：{task_name}。请查看 MoviePilot 日志。")
        except Exception as error:
            logger.exception(f"馒头保活：提交一次性任务失败：{error}")
            self._set_status(f"提交一次性任务失败：{error}")

    @staticmethod
    def _now_text() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    def _set_status(self, message: str, *, last_run: bool = True) -> None:
        self._status = message
        try:
            self.save_data("status", message)
            if last_run:
                self.save_data("last_run", self._now_text())
        except Exception as error:
            logger.warning(f"馒头保活：保存运行状态失败：{error}")

    @staticmethod
    def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _validate_site_url(value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return MTeamKeepAlive.DEFAULT_SITE_URL
        return value.rstrip("/") + "/"

    def _data_files(self) -> tuple[Path, Path]:
        data_dir = self.get_data_path()
        try:
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(data_dir, 0o700)
        except OSError:
            pass
        return data_dir / "storage_state.json", data_dir / "session_storage.json"

    @staticmethod
    def _write_private_json(path: Path, value: Any) -> None:
        """以临时文件原子替换方式保存包含登录凭据的 JSON。"""
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        temp_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load_storage_state(self) -> dict[str, Any] | None:
        state_file, _ = self._data_files()
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as error:
            logger.warning(f"馒头保活：读取浏览器状态失败，将使用空白会话：{error}")
            return None

    def _install_session_storage(self, context: Any) -> None:
        """Playwright storage_state 不包含 sessionStorage，这里单独恢复站点会话存储。"""
        _, session_file = self._data_files()
        if not session_file.exists():
            return
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8"))
            origin = str(payload.get("origin") or "")
            values = payload.get("values") or {}
            if not origin or not isinstance(values, dict):
                return
            script = (
                "(() => {"
                f"const wantedOrigin = {json.dumps(origin)};"
                f"const saved = {json.dumps(values, ensure_ascii=False)};"
                "if (window.location.origin !== wantedOrigin) return;"
                "for (const [key, value] of Object.entries(saved)) {"
                "try { window.sessionStorage.setItem(key, String(value)); } catch (_) {}"
                "}"
                "})();"
            )
            context.add_init_script(script)
        except Exception as error:
            logger.warning(f"馒头保活：恢复 sessionStorage 失败：{error}")

    def _save_browser_state(self, context: Any, page: Any) -> None:
        state_file, session_file = self._data_files()
        storage_state = context.storage_state()
        self._write_private_json(state_file, storage_state)

        try:
            session_payload = page.evaluate(
                "() => ({origin: location.origin, values: Object.fromEntries(" 
                "Array.from({length: sessionStorage.length}, (_, i) => {" 
                "const key = sessionStorage.key(i); return [key, sessionStorage.getItem(key)];" 
                "}))})"
            )
            if isinstance(session_payload, dict):
                self._write_private_json(session_file, session_payload)
        except Exception as error:
            logger.warning(f"馒头保活：保存 sessionStorage 失败：{error}")

    def _launch_context(self, *, headless: bool) -> Any:
        """加载已保存的 cookies/localStorage，并补充 sessionStorage。"""
        storage_state = self._load_storage_state()
        try:
            context = launch_browser_context(
                headless=headless,
                storage_state=storage_state,
            )
        except Exception:
            if not storage_state:
                raise
            logger.warning("馒头保活：带登录态启动浏览器失败，改用空白会话。")
            context = launch_browser_context(headless=headless)
        self._install_session_storage(context)
        return context

    # ---------------------------------------------------------------------
    # 人工登录
    # ---------------------------------------------------------------------
    def open_login_browser(self) -> None:
        """打开有界面浏览器，人工完成站点登录后自动保存浏览器状态。"""
        if not self._run_lock.acquire(blocking=False):
            self._set_status("已有浏览器任务正在运行，本次登录窗口未启动。")
            return
        context = None
        try:
            if self._stopping:
                return
            self._set_status("正在启动人工登录浏览器……", last_run=False)
            context = self._launch_context(headless=False)
            page = context.new_page()
            page.goto(self._site_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_000)
            self._set_status(
                f"登录浏览器已打开。请在浏览器中手动完成邮箱验证码登录；保持窗口打开约 "
                f"{self._login_wait_minutes} 分钟，插件随后自动保存 cookies、localStorage 和 sessionStorage。",
                last_run=False,
            )
            deadline = time.monotonic() + self._login_wait_minutes * 60
            while time.monotonic() < deadline and not self._stopping:
                if page.is_closed():
                    break
                time.sleep(1)

            if page.is_closed():
                self._set_status("登录窗口已关闭，未能从关闭后的页面读取完整状态；请重新打开登录窗口并保持页面。")
                return

            self._save_browser_state(context, page)
            self._set_status(
                "浏览器状态已保存。请勾选“立即执行一次保活”验证登录态；保存成功不等于站点已确认登录有效。"
            )
            logger.info("馒头保活：人工登录窗口结束，浏览器状态已保存（凭据未写入日志）。")
        except Exception as error:
            logger.exception(f"馒头保活：人工登录流程失败：{error}")
            self._set_status(f"人工登录流程失败：{error}")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception as error:
                    logger.warning(f"馒头保活：关闭登录浏览器失败：{error}")
            self._run_lock.release()

    # ---------------------------------------------------------------------
    # 定时保活
    # ---------------------------------------------------------------------
    def keepalive(self) -> None:
        """以无头浏览器访问站点首页，更新并保存 cookies 与站点存储。"""
        if self._stopping:
            return
        if not self._run_lock.acquire(blocking=False):
            logger.info("馒头保活：已有浏览器任务运行，跳过本轮。")
            return

        context = None
        try:
            state_file, _ = self._data_files()
            if not state_file.exists():
                self._set_status("尚未保存登录态。请先勾选“打开人工登录窗口”并完成登录。")
                return

            self._set_status("正在访问馒头站点……", last_run=False)
            context = self._launch_context(headless=True)
            page = context.new_page()
            response = page.goto(
                self._site_url,
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            page.wait_for_timeout(5_000)

            current_url = str(page.url or "")
            title = str(page.title() or "")
            login_form = page.locator(
                'input[type="password"], input[autocomplete="one-time-code"]'
            ).count() > 0
            challenge_title = title.lower()
            challenge = any(
                marker in challenge_title
                for marker in ("just a moment", "checking your browser", "security check")
            ) or "请稍候" in title or "安全验证" in title

            if challenge:
                raise RuntimeError("站点返回安全验证页面；本插件不会自动绕过验证，请先通过人工登录窗口完成验证。")
            if login_form or any(marker in current_url.lower() for marker in ("/login", "/signin", "/sign-in")):
                raise RuntimeError("访问后出现登录页面，已保存的登录态可能失效；请重新进行人工登录。")
            if response is not None and int(response.status) >= 400:
                raise RuntimeError(f"站点返回 HTTP {response.status}")

            self._save_browser_state(context, page)
            status_code = int(response.status) if response is not None else "未知"
            self._set_status(
                f"访问完成：HTTP {status_code}，页面标题“{title[:80]}”。未检测到明显的登录表单或登录跳转；"
                "这只能证明页面访问成功，不能替代站点侧的账号状态确认。"
            )
            logger.info(f"馒头保活：站点访问完成，HTTP {status_code}，登录态文件已更新。")
        except Exception as error:
            logger.exception(f"馒头保活：本轮访问失败：{error}")
            self._set_status(f"本轮访问失败：{error}")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception as error:
                    logger.warning(f"馒头保活：关闭保活浏览器失败：{error}")
            self._run_lock.release()
