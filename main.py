import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "newapi",
    "木有知",
    "NewAPI 运维助手：概览/模型/日志/额度/异常/分析/建议/健康（中文简指令）",
    "2.1.0",
)
class NewAPIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.base_domain: str = str(config.get("base_domain", "")).strip().rstrip("/")
        self.authorization: str = str(config.get("authorization", "")).strip()
        self.new_api_user: str = str(config.get("new_api_user", "")).strip()
        self.request_timeout: int = int(config.get("request_timeout", 15) or 15)

        self.default_window_hours: int = int(config.get("default_window_hours", 24) or 24)
        self.default_top_n: int = int(config.get("default_top_n", 5) or 5)
        self.default_log_limit: int = int(config.get("log_page_size", 20) or 20)

        self.use_forward: bool = bool(config.get("use_forward", True))
        self.log_use_forward: bool = bool(config.get("log_use_forward", self.use_forward))
        self.user_use_forward: bool = bool(config.get("user_use_forward", False))

        self.llm_enabled: bool = bool(config.get("llm_enabled", False))
        self.llm_use_current_provider: bool = bool(config.get("llm_use_current_provider", True))
        self.llm_provider_id: str = str(config.get("llm_provider_id", "")).strip()

        self._setup_data_paths()

    def _setup_data_paths(self):
        plugin_name = "newapi"
        self.plugin_data_dir = Path(__file__).resolve().parent / "data"
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore

            self.plugin_data_dir = get_astrbot_data_path() / "plugin_data" / plugin_name
        except Exception:
            pass
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.plugin_data_dir / "last_usage_payload.json"

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.authorization:
            h["Authorization"] = self.authorization
        if self.new_api_user:
            h["New-Api-User"] = self.new_api_user
        return h

    async def _http_get_json(self, url: str, headers: Dict[str, str]) -> Any:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        def _do() -> Any:
            req = Request(url=url, method="GET")
            for k, v in headers.items():
                req.add_header(k, v)
            with urlopen(req, timeout=self.request_timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return json.loads(body)

        try:
            return await asyncio.to_thread(_do)
        except HTTPError as e:
            return {"error": f"HTTP {e.code}"}
        except URLError as e:
            return {"error": f"URL {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def _http_post_json(self, url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout_sec: int) -> Any:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        def _do() -> Any:
            req = Request(url=url, method="POST")
            for k, v in headers.items():
                req.add_header(k, v)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            with urlopen(req, data=data, timeout=timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return json.loads(body)

        try:
            return await asyncio.to_thread(_do)
        except HTTPError as e:
            return {"error": f"HTTP {e.code}"}
        except URLError as e:
            return {"error": f"URL {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    def _extract_records(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "list", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("data", "list", "items"):
                    vv = v.get(k2)
                    if isinstance(vv, list):
                        return vv
        return []

    def _fmt_ts(self, ts: int) -> str:
        tz = timezone(timedelta(hours=8), name="CST+8")
        return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")

    def _window(self, hours: int) -> Tuple[int, int]:
        end_ts = int(time.time())
        start_ts = end_ts - int(hours) * 3600
        return start_ts, end_ts

    async def _fetch_usage_payload(self, hours: int) -> Any:
        if not self.base_domain:
            return {"error": "missing base_domain"}
        start_ts, end_ts = self._window(hours)
        q = urlencode(
            {
                "username": "",
                "start_timestamp": str(start_ts),
                "end_timestamp": str(end_ts),
                "default_time": "hour",
            }
        )
        url = f"{self.base_domain}/api/data/self?{q}"
        payload = await self._http_get_json(url, self._headers())
        if isinstance(payload, (dict, list)) and not (isinstance(payload, dict) and payload.get("error")):
            try:
                self.cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return payload

    async def _fetch_logs_payload(self, limit: int, hours: int = 24) -> Any:
        if not self.base_domain:
            return {"error": "missing base_domain"}
        start_ts, end_ts = self._window(hours)
        q = urlencode(
            {
                "p": 1,
                "page_size": max(1, min(limit, 100)),
                "type": 0,
                "start_timestamp": str(start_ts),
                "end_timestamp": str(end_ts),
            }
        )
        url = f"{self.base_domain}/api/log/?{q}"
        return await self._http_get_json(url, self._headers())

    async def _fetch_user_self(self) -> Any:
        if not self.base_domain:
            return {"error": "missing base_domain"}
        return await self._http_get_json(f"{self.base_domain}/api/user/self", self._headers())

    def _aggregate(self, records: List[Dict[str, Any]], start_ts: int, end_ts: int) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, int]]]]:
        total_tokens = 0
        total_requests = 0
        total_quota = 0
        model_stats: Dict[str, Dict[str, int]] = {}

        for r in records:
            ts = int(r.get("created_at", 0) or 0)
            if ts < start_ts or ts > end_ts:
                continue
            model = str(r.get("model_name") or "未知模型")
            token = int(r.get("token_used", 0) or 0)
            cnt = int(r.get("count", 0) or 0)
            quota = int(r.get("quota", 0) or 0)

            total_tokens += token
            total_requests += cnt
            total_quota += quota

            s = model_stats.setdefault(model, {"token": 0, "count": 0, "quota": 0})
            s["token"] += token
            s["count"] += cnt
            s["quota"] += quota

        minutes = max(1, int((end_ts - start_ts) / 60))
        stats = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "tokens": total_tokens,
            "requests": total_requests,
            "quota": total_quota,
            "rpm": total_requests / minutes,
            "tpm": total_tokens / minutes,
            "minutes": minutes,
        }
        sorted_models = sorted(model_stats.items(), key=lambda kv: kv[1]["count"], reverse=True)
        return stats, sorted_models

    def _format_overview(self, stats: Dict[str, Any], top_models: List[Tuple[str, Dict[str, int]]], top_n: int) -> str:
        lines = [
            "📊 NewAPI 概览",
            f"时间: {self._fmt_ts(stats['start_ts'])} - {self._fmt_ts(stats['end_ts'])}",
            f"总 tokens: {stats['tokens']:,}",
            f"总请求: {stats['requests']:,}",
            f"总配额: {stats['quota']:,}",
            f"平均 RPM: {stats['rpm']:.3f}",
            f"平均 TPM: {stats['tpm']:.3f}",
        ]
        if top_models and top_n > 0:
            lines.append(f"\n🔥 Top{top_n} 模型:")
            for i, (m, s) in enumerate(top_models[:top_n], 1):
                lines.append(f"{i}. {m} | 请求{s['count']:,} | token{s['token']:,}")
        return "\n".join(lines)

    def _extract_log_items(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        d = payload.get("data")
        if isinstance(d, dict):
            for k in ("items", "list", "data"):
                v = d.get(k)
                if isinstance(v, list):
                    return v
        for k in ("items", "list"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        return []

    def _format_logs(self, items: List[Dict[str, Any]]) -> str:
        out = [f"📜 最近日志（{len(items)}条）"]
        for it in items:
            t = int(it.get("created_at", 0) or 0)
            m = str(it.get("model_name") or "未知模型")
            typ = int(it.get("type", 0) or 0)
            status = int(it.get("code", 0) or 0)
            pt = int(it.get("prompt_tokens", 0) or 0)
            ct = int(it.get("completion_tokens", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            out.append(
                f"- {self._fmt_ts(t)} | {m} | type={typ} code={status} | in={pt} out={ct} | {use}ms"
            )
        return "\n".join(out)

    def _detect_abnormal(self, items: List[Dict[str, Any]]) -> str:
        errs = []
        slow = []
        for it in items:
            code = int(it.get("code", 0) or 0)
            typ = int(it.get("type", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            if typ == 5 or code >= 400:
                errs.append(it)
            if use >= 15000:
                slow.append(it)

        lines = ["🚨 异常分析"]
        lines.append(f"错误条数: {len(errs)}")
        lines.append(f"慢请求(>=15s): {len(slow)}")
        if errs:
            lines.append("\n最近错误:")
            for it in errs[:5]:
                t = int(it.get("created_at", 0) or 0)
                m = str(it.get("model_name") or "未知模型")
                code = int(it.get("code", 0) or 0)
                lines.append(f"- {self._fmt_ts(t)} | {m} | code={code}")
        return "\n".join(lines)

    async def _llm_analyze(self, event: AstrMessageEvent, title: str, content: str) -> str:
        if not self.llm_enabled:
            return "未开启 LLM 分析（请在配置中启用 llm_enabled）"

        # 默认使用当前会话服务商；可切换为手动指定 provider
        provider_id = ""
        try:
            if self.llm_use_current_provider:
                provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            else:
                provider_id = self.llm_provider_id
        except Exception as e:
            return f"获取服务商失败: {e}"

        if not provider_id:
            return "LLM 服务商未设置（请检查 llm_use_current_provider 或 llm_provider_id）"

        prompt = (
            f"你是 NewAPI 运维分析助手。请基于以下数据输出中文简报，结构固定为：\n"
            f"1) 主要问题（最多3条）\n2) 根因判断\n3) 立即动作（P0/P1）\n4) 优化建议\n\n"
            f"[{title}]\n{content}"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return str(llm_resp.completion_text).strip()
        except Exception as e:
            return f"LLM 调用失败: {e}"

    async def _send_text(self, event: AstrMessageEvent, text: str, use_forward: bool):
        if use_forward:
            try:
                node = Comp.Node(uin=10000, name="newapi", content=[Comp.Plain(text)])
                yield event.chain_result([node])
                return
            except Exception:
                pass
        max_len = 900
        t = text
        while t:
            c = t[:max_len]
            t = t[max_len:]
            yield event.plain_result(c)

    @filter.command("newapi")
    async def cmd_newapi_help(self, event: AstrMessageEvent):
        text = (
            "📘 NewAPI 指令\n"
            "/概览 [小时]  /模型 [topN]  /日志 [条数]\n"
            "/额度  /异常  /分析  /建议  /健康"
        )
        async for r in self._send_text(event, text, False):
            yield r

    @filter.command("概览", alias={"tokens统计", "newapi概览"})
    async def cmd_overview(self, event: AstrMessageEvent, hours: int = 24):
        hours = max(1, min(hours, 168))
        payload = await self._fetch_usage_payload(hours)
        records = self._extract_records(payload)

        # 回退本地缓存
        if not records and self.cache_file.exists():
            try:
                records = self._extract_records(json.loads(self.cache_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        s, e = self._window(hours)
        stats, models = self._aggregate(records, s, e)
        text = self._format_overview(stats, models, self.default_top_n)
        async for r in self._send_text(event, text, self.use_forward):
            yield r

    @filter.command("模型", alias={"模型排行"})
    async def cmd_models(self, event: AstrMessageEvent, topn: int = 5):
        topn = max(1, min(topn, 20))
        payload = await self._fetch_usage_payload(self.default_window_hours)
        records = self._extract_records(payload)
        s, e = self._window(self.default_window_hours)
        stats, models = self._aggregate(records, s, e)
        text = self._format_overview(stats, models, topn)
        async for r in self._send_text(event, text, self.use_forward):
            yield r

    @filter.command("日志", alias={"logs"})
    async def cmd_logs(self, event: AstrMessageEvent, n: int = 20):
        n = max(1, min(n, 100))
        payload = await self._fetch_logs_payload(n, self.default_window_hours)
        items = self._extract_log_items(payload)
        text = self._format_logs(items)
        async for r in self._send_text(event, text, self.log_use_forward):
            yield r

    @filter.command("额度", alias={"查询额度"})
    async def cmd_quota(self, event: AstrMessageEvent):
        p = await self._fetch_user_self()
        if isinstance(p, dict) and isinstance(p.get("data"), dict):
            d = p["data"]
            quota = int(d.get("quota", 0) or 0)
            used = int(d.get("used_quota", 0) or 0)
            req = int(d.get("request_count", 0) or 0)
            text = (
                "💳 账户额度\n"
                f"用户名: {d.get('username', '-') }\n"
                f"分组: {d.get('group', '-') }\n"
                f"请求次数: {req:,}\n"
                f"已用配额: {used:,}\n"
                f"当前额度(配额/500): $ {quota/500:.2f}"
            )
        else:
            text = f"查询失败: {json.dumps(p, ensure_ascii=False)[:400]}"
        async for r in self._send_text(event, text, self.user_use_forward):
            yield r

    @filter.command("异常")
    async def cmd_abnormal(self, event: AstrMessageEvent):
        payload = await self._fetch_logs_payload(max(self.default_log_limit, 30), 24)
        items = self._extract_log_items(payload)
        text = self._detect_abnormal(items)
        async for r in self._send_text(event, text, False):
            yield r

    @filter.command("分析")
    async def cmd_analysis(self, event: AstrMessageEvent):
        usage = await self._fetch_usage_payload(self.default_window_hours)
        logs = await self._fetch_logs_payload(max(self.default_log_limit, 30), 24)
        usage_records = self._extract_records(usage)
        log_items = self._extract_log_items(logs)
        s, e = self._window(self.default_window_hours)
        stats, models = self._aggregate(usage_records, s, e)

        brief = {
            "stats": stats,
            "top_models": models[:5],
            "abnormal_preview": self._detect_abnormal(log_items),
        }
        text = await self._llm_analyze(event, "调用分析", json.dumps(brief, ensure_ascii=False))
        async for r in self._send_text(event, text, False):
            yield r

    @filter.command("建议")
    async def cmd_advice(self, event: AstrMessageEvent):
        logs = await self._fetch_logs_payload(max(self.default_log_limit, 50), 24)
        log_items = self._extract_log_items(logs)
        raw = self._detect_abnormal(log_items)
        text = await self._llm_analyze(event, "优化建议", raw)
        async for r in self._send_text(event, text, False):
            yield r

    @filter.command("健康", alias={"health"})
    async def cmd_health(self, event: AstrMessageEvent):
        out = ["🩺 健康检查"]
        out.append(f"base_domain: {'OK' if self.base_domain else '缺失'}")
        out.append(f"authorization: {'OK' if self.authorization else '缺失'}")
        out.append(f"new_api_user: {'OK' if self.new_api_user else '缺失'}")

        if self.base_domain:
            p1 = await self._fetch_user_self()
            out.append(f"/api/user/self: {'OK' if isinstance(p1, dict) and not p1.get('error') else 'FAIL'}")
            p2 = await self._fetch_logs_payload(1, 1)
            out.append(f"/api/log/: {'OK' if isinstance(p2, dict) and not p2.get('error') else 'FAIL'}")
            p3 = await self._fetch_usage_payload(1)
            out.append(f"/api/data/self: {'OK' if isinstance(p3, dict) and not p3.get('error') else 'FAIL'}")

        if self.llm_enabled:
            if self.llm_use_current_provider:
                out.append("LLM: 已启用（使用当前会话服务商）")
            else:
                out.append(f"LLM: 已启用（手动服务商: {self.llm_provider_id or '未设置'}）")
        else:
            out.append("LLM: 未启用")

        async for r in self._send_text(event, "\n".join(out), False):
            yield r

    async def terminate(self):
        logger.info("newapi 插件已卸载")
