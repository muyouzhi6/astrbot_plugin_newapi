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
    "2.2.0",
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
        if payload.get("success") is False and payload.get("message"):
            logger.warning(f"newapi usage api failed: {payload.get('message')}")
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
        if payload.get("success") is False and payload.get("message"):
            logger.warning(f"newapi log api failed: {payload.get('message')}")
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
        if not items:
            return "📜 调用日志\n暂无数据（可能是时间窗口内无请求或接口未返回明细）"

        total = len(items)
        err_items = [it for it in items if int(it.get("code", 0) or 0) >= 400 or int(it.get("type", 0) or 0) == 5]
        slow5_items = [it for it in items if int(it.get("use_time", 0) or 0) >= 5000]
        slow15_items = [it for it in items if int(it.get("use_time", 0) or 0) >= 15000]
        avg_use = int(sum(int(it.get("use_time", 0) or 0) for it in items) / max(1, total))

        model_cnt: Dict[str, int] = {}
        for it in items:
            m = str(it.get("model_name") or "未知模型")
            model_cnt[m] = model_cnt.get(m, 0) + 1
        top_models = sorted(model_cnt.items(), key=lambda kv: kv[1], reverse=True)[:3]

        out = ["📜 调用日志总览"]
        out.append(
            f"总请求 {total} | 错误 {len(err_items)} ({len(err_items)/max(1,total):.1%}) | "
            f"慢请求>=5s {len(slow5_items)} | 超慢>=15s {len(slow15_items)} | 平均耗时 {avg_use}ms"
        )
        if top_models:
            out.append("主力模型: " + "，".join([f"{m}({c})" for m, c in top_models]))

        out.append("\n🧾 最近明细（新→旧）")
        for it in items[:20]:
            t = int(it.get("created_at", 0) or 0)
            m = str(it.get("model_name") or "未知模型")
            typ = int(it.get("type", 0) or 0)
            code = int(it.get("code", 0) or 0)
            pt = int(it.get("prompt_tokens", 0) or 0)
            ct = int(it.get("completion_tokens", 0) or 0)
            use = int(it.get("use_time", 0) or 0)

            if typ == 5 or code >= 500:
                icon = "🔴"
            elif code >= 400:
                icon = "🟠"
            else:
                icon = "🟢"

            if use >= 15000:
                lat = "🐢"
            elif use >= 5000:
                lat = "⚠️"
            else:
                lat = "⚡"

            out.append(
                f"{icon} {self._fmt_ts(t)} | {m} | code={code} | {lat}{use}ms | token {pt}/{ct}"
            )

        if total > 20:
            out.append(f"… 其余 {total-20} 条已省略，可用 /日志 {min(100, total)} 查看更多")

        return "\n".join(out)

    def _detect_abnormal(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "🚨 异常分析\n暂无日志数据，无法判断。"

        errs: List[Dict[str, Any]] = []
        slow: List[Dict[str, Any]] = []
        model_err: Dict[str, int] = {}

        for it in items:
            code = int(it.get("code", 0) or 0)
            typ = int(it.get("type", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            m = str(it.get("model_name") or "未知模型")
            if typ == 5 or code >= 400:
                errs.append(it)
                model_err[m] = model_err.get(m, 0) + 1
            if use >= 15000:
                slow.append(it)

        total = len(items)
        err_rate = len(errs) / max(1, total)
        slow_rate = len(slow) / max(1, total)

        lines = ["🚨 异常分析"]
        lines.append(
            f"总请求 {total} | 错误 {len(errs)} ({err_rate:.1%}) | 超慢>=15s {len(slow)} ({slow_rate:.1%})"
        )

        if err_rate >= 0.2:
            lvl = "P0"
            reason = "错误率过高，已显著影响可用性"
        elif err_rate >= 0.08 or len(slow) >= 5:
            lvl = "P1"
            reason = "稳定性退化，建议尽快处理"
        elif err_rate > 0 or len(slow) > 0:
            lvl = "P2"
            reason = "存在零星异常，建议观察并优化"
        else:
            lvl = "OK"
            reason = "未发现明显异常"
        lines.append(f"风险等级: {lvl}（{reason}）")

        if model_err:
            top_err = sorted(model_err.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("高风险模型: " + "，".join([f"{m}({c})" for m, c in top_err]))

        if errs:
            lines.append("\n🧯 最近错误样本")
            for it in errs[:5]:
                t = int(it.get("created_at", 0) or 0)
                m = str(it.get("model_name") or "未知模型")
                code = int(it.get("code", 0) or 0)
                use = int(it.get("use_time", 0) or 0)
                lines.append(f"- {self._fmt_ts(t)} | {m} | code={code} | {use}ms")

        if slow:
            lines.append("\n🐢 超慢样本")
            for it in slow[:3]:
                t = int(it.get("created_at", 0) or 0)
                m = str(it.get("model_name") or "未知模型")
                use = int(it.get("use_time", 0) or 0)
                lines.append(f"- {self._fmt_ts(t)} | {m} | {use}ms")

        lines.append("\n✅ 建议动作")
        if lvl in ("P0", "P1"):
            lines.append("1) 先限制异常模型并切换备用模型验证")
            lines.append("2) 缩短 max_tokens/降低并发，观察 15 分钟")
            lines.append("3) 按 code 分组排查上游网关与 provider 状态")
        elif lvl == "P2":
            lines.append("1) 针对慢请求模型做参数收敛（max_tokens/temperature）")
            lines.append("2) 保持监控，若错误率升至 8% 以上按 P1 处理")
        else:
            lines.append("1) 当前健康，可继续观察峰值时段")

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
            "你是资深 NewAPI SRE 值班工程师。请基于输入数据输出【可执行】中文运维结论，禁止空话。\n\n"
            "输出必须严格按以下结构：\n"
            "# 结论摘要\n"
            "- 一句话判断当前系统状态（健康/亚健康/故障）\n"
            "- 影响范围（用户面/模型面/时段）\n\n"
            "# 关键发现（按严重度排序，最多5条）\n"
            "每条格式：\n"
            "- [P0|P1|P2] 现象｜证据（具体数值）｜可能根因\n\n"
            "# 立即动作（15分钟内）\n"
            "列 3-5 条可直接执行动作，每条都要有目标与预期\n\n"
            "# 今日优化（当天完成）\n"
            "列 3-5 条优化项，优先稳定性与成本\n\n"
            "# 观察指标与阈值\n"
            "至少给出：错误率、P95耗时、超慢占比、请求量波动阈值，并写明告警阈值\n\n"
            "# 需要补充的数据\n"
            "若数据不足，明确缺什么，不得臆测。\n\n"
            f"【分析主题】{title}\n"
            f"【输入数据】\n{content}\n"
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
            "/额度  /异常  /分析  /建议  /健康\n"
            "\n"
            "💡 LLM 服务商：\n"
            "- 默认使用当前会话服务商（llm_use_current_provider=true）\n"
            "- 关闭后可在 llm_provider_id 下拉指定"
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
        async for r in self._send_text(event, text, self.log_use_forward):
            yield r

    @filter.command("分析")
    async def cmd_analysis(self, event: AstrMessageEvent):
        usage = await self._fetch_usage_payload(self.default_window_hours)
        logs = await self._fetch_logs_payload(max(self.default_log_limit, 30), 24)
        usage_records = self._extract_records(usage)
        log_items = self._extract_log_items(logs)
        s, e = self._window(self.default_window_hours)
        stats, models = self._aggregate(usage_records, s, e)

        err_cnt = 0
        slow_cnt = 0
        for it in log_items:
            code = int(it.get("code", 0) or 0)
            typ = int(it.get("type", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            if typ == 5 or code >= 400:
                err_cnt += 1
            if use >= 15000:
                slow_cnt += 1

        brief = {
            "window_hours": self.default_window_hours,
            "summary": {
                "tokens": stats.get("tokens", 0),
                "requests": stats.get("requests", 0),
                "quota": stats.get("quota", 0),
                "rpm": round(float(stats.get("rpm", 0)), 4),
                "tpm": round(float(stats.get("tpm", 0)), 4),
            },
            "top_models": [
                {
                    "model": m,
                    "requests": s.get("count", 0),
                    "tokens": s.get("token", 0),
                    "quota": s.get("quota", 0),
                }
                for m, s in models[:8]
            ],
            "log_snapshot": {
                "total": len(log_items),
                "error_count": err_cnt,
                "error_rate": round(err_cnt / max(1, len(log_items)), 4),
                "slow15s_count": slow_cnt,
                "slow15s_rate": round(slow_cnt / max(1, len(log_items)), 4),
            },
            "recent_errors": [
                {
                    "time": self._fmt_ts(int(it.get("created_at", 0) or 0)),
                    "model": str(it.get("model_name") or "未知模型"),
                    "code": int(it.get("code", 0) or 0),
                    "use_time_ms": int(it.get("use_time", 0) or 0),
                }
                for it in log_items
                if int(it.get("type", 0) or 0) == 5 or int(it.get("code", 0) or 0) >= 400
            ][:8],
        }
        text = await self._llm_analyze(event, "调用分析", json.dumps(brief, ensure_ascii=False))
        async for r in self._send_text(event, text, self.use_forward):
            yield r

    @filter.command("建议")
    async def cmd_advice(self, event: AstrMessageEvent):
        logs = await self._fetch_logs_payload(max(self.default_log_limit, 50), 24)
        log_items = self._extract_log_items(logs)
        raw = self._detect_abnormal(log_items)
        text = await self._llm_analyze(event, "优化建议", raw)
        async for r in self._send_text(event, text, self.use_forward):
            yield r

    @filter.command("健康", alias={"health"})
    async def cmd_health(self, event: AstrMessageEvent):
        out = ["🩺 健康检查"]
        out.append(f"plugin_version: 2.2.0")
        out.append(f"base_domain: {'OK' if self.base_domain else '缺失'}")
        out.append(f"authorization: {'OK' if self.authorization else '缺失'}")
        out.append(f"new_api_user: {'OK' if self.new_api_user else '缺失'}")

        if self.base_domain:
            p1 = await self._fetch_user_self()
            ok1 = isinstance(p1, dict) and not p1.get('error') and p1.get('success', True)
            out.append(f"/api/user/self: {'OK' if ok1 else 'FAIL'}")
            p2 = await self._fetch_logs_payload(1, 1)
            ok2 = isinstance(p2, dict) and not p2.get('error') and p2.get('success', True)
            out.append(f"/api/log/: {'OK' if ok2 else 'FAIL'}")
            p3 = await self._fetch_usage_payload(1)
            ok3 = isinstance(p3, dict) and not p3.get('error') and p3.get('success', True)
            out.append(f"/api/data/self: {'OK' if ok3 else 'FAIL'}")

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
