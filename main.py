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
    "2.3.1",
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

    def _aggregate_by_keys(
        self,
        records: List[Dict[str, Any]],
        start_ts: int,
        end_ts: int,
        key_candidates: List[str],
        fallback: str = "未知",
    ) -> List[Tuple[str, Dict[str, int]]]:
        stats: Dict[str, Dict[str, int]] = {}
        for r in records:
            ts = int(r.get("created_at", 0) or 0)
            if ts < start_ts or ts > end_ts:
                continue

            k = fallback
            for key in key_candidates:
                v = r.get(key)
                if v not in (None, "", 0):
                    k = str(v)
                    break

            token = int(r.get("token_used", 0) or 0)
            cnt = int(r.get("count", 0) or 0)
            quota = int(r.get("quota", 0) or 0)
            s = stats.setdefault(k, {"token": 0, "count": 0, "quota": 0})
            s["token"] += token
            s["count"] += cnt
            s["quota"] += quota

        return sorted(stats.items(), key=lambda kv: kv[1]["token"], reverse=True)

    def _percentile(self, nums: List[int], q: float) -> int:
        if not nums:
            return 0
        arr = sorted(nums)
        idx = min(len(arr) - 1, max(0, int((len(arr) - 1) * q)))
        return int(arr[idx])

    def _summarize_log_metrics(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = len(items)
        lat = [int(it.get("use_time", 0) or 0) for it in items]
        err = [it for it in items if int(it.get("type", 0) or 0) == 5 or int(it.get("code", 0) or 0) >= 400]
        slow = [it for it in items if int(it.get("use_time", 0) or 0) >= 15000]

        code_dist: Dict[str, int] = {}
        model_dist: Dict[str, int] = {}
        chan_dist: Dict[str, int] = {}
        for it in items:
            c = str(int(it.get("code", 0) or 0))
            code_dist[c] = code_dist.get(c, 0) + 1

            m = str(it.get("model_name") or "未知模型")
            model_dist[m] = model_dist.get(m, 0) + 1

            chan = str(
                it.get("channel_name")
                or it.get("channel")
                or it.get("channel_id")
                or it.get("provider_name")
                or it.get("provider")
                or it.get("provider_id")
                or "未知渠道"
            )
            chan_dist[chan] = chan_dist.get(chan, 0) + 1

        return {
            "total": total,
            "err_count": len(err),
            "err_rate": len(err) / max(1, total),
            "slow15_count": len(slow),
            "slow15_rate": len(slow) / max(1, total),
            "avg_ms": int(sum(lat) / max(1, total)) if lat else 0,
            "p50_ms": self._percentile(lat, 0.5),
            "p95_ms": self._percentile(lat, 0.95),
            "p99_ms": self._percentile(lat, 0.99),
            "code_top": sorted(code_dist.items(), key=lambda kv: kv[1], reverse=True)[:6],
            "model_top": sorted(model_dist.items(), key=lambda kv: kv[1], reverse=True)[:5],
            "channel_top": sorted(chan_dist.items(), key=lambda kv: kv[1], reverse=True)[:5],
            "err_items": err,
            "slow_items": sorted(items, key=lambda x: int(x.get("use_time", 0) or 0), reverse=True)[:8],
        }

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
            total_token = max(1, int(stats.get("tokens", 0) or 0))
            for i, (m, s) in enumerate(top_models[:top_n], 1):
                pct = (int(s['token']) / total_token) * 100
                lines.append(f"{i}. {m} | 请求{s['count']:,} | token{s['token']:,} ({pct:.1f}%)")
        return "\n".join(lines)

    def _format_dual_window_report(
        self,
        stats_24: Dict[str, Any],
        models_24: List[Tuple[str, Dict[str, int]]],
        channels_24: List[Tuple[str, Dict[str, int]]],
        stats_2: Dict[str, Any],
        models_2: List[Tuple[str, Dict[str, int]]],
        channels_2: List[Tuple[str, Dict[str, int]]],
        log_chan_24: List[Tuple[str, int]],
        log_chan_2: List[Tuple[str, int]],
    ) -> str:
        def _line(name: str, s: Dict[str, int], total_token: int) -> str:
            pct = (int(s.get("token", 0)) / max(1, total_token)) * 100
            return f"{name} | token {int(s.get('token',0)):,} ({pct:.1f}%) | req {int(s.get('count',0)):,}"

        out = ["📈 消耗对比（24h vs 2h）"]
        out.append(
            f"24h: token {stats_24['tokens']:,} | req {stats_24['requests']:,} | quota {stats_24['quota']:,} | RPM {stats_24['rpm']:.2f}"
        )
        out.append(
            f"2h : token {stats_2['tokens']:,} | req {stats_2['requests']:,} | quota {stats_2['quota']:,} | RPM {stats_2['rpm']:.2f}"
        )

        out.append("\n🤖 24h 模型集中度")
        for m, s in models_24[:5]:
            out.append("- " + _line(m, s, int(stats_24['tokens'])))

        out.append("\n🤖 2h 模型集中度")
        if models_2:
            for m, s in models_2[:5]:
                out.append("- " + _line(m, s, int(stats_2['tokens'])))
        else:
            out.append("- 暂无数据")

        usage_chan_24_valid = any(c != "未知渠道" for c, _ in channels_24)
        usage_chan_2_valid = any(c != "未知渠道" for c, _ in channels_2)

        out.append("\n🛣️ 24h 渠道集中度")
        if usage_chan_24_valid:
            for c, s in channels_24[:5]:
                out.append("- " + _line(c, s, int(stats_24['tokens'])))
        elif log_chan_24:
            total_req_24 = max(1, sum(n for _, n in log_chan_24))
            out.append("- usage 接口缺少渠道字段，以下基于日志请求数")
            for c, n in log_chan_24[:5]:
                out.append(f"- {c} | req {n:,} ({n/total_req_24:.1%})")
        else:
            out.append("- 暂无渠道数据")

        out.append("\n🛣️ 2h 渠道集中度")
        if usage_chan_2_valid:
            for c, s in channels_2[:5]:
                out.append("- " + _line(c, s, int(stats_2['tokens'])))
        elif log_chan_2:
            total_req_2 = max(1, sum(n for _, n in log_chan_2))
            out.append("- usage 接口缺少渠道字段，以下基于日志请求数")
            for c, n in log_chan_2[:5]:
                out.append(f"- {c} | req {n:,} ({n/total_req_2:.1%})")
        else:
            out.append("- 暂无渠道数据")

        if stats_24['tokens'] > 0:
            ratio = (stats_2['tokens'] / stats_24['tokens']) * 100
            out.append(f"\n🔎 近2h token占24h比例: {ratio:.1f}%")

        return "\n".join(out)

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

        m = self._summarize_log_metrics(items)

        out = ["📜 调用日志总览"]
        out.append(
            f"总请求 {m['total']} | 错误 {m['err_count']} ({m['err_rate']:.1%}) | 超慢>=15s {m['slow15_count']} ({m['slow15_rate']:.1%})"
        )
        out.append(
            f"耗时: avg {m['avg_ms']}ms | p50 {m['p50_ms']}ms | p95 {m['p95_ms']}ms | p99 {m['p99_ms']}ms"
        )

        if m["code_top"]:
            out.append("状态码分布: " + "，".join([f"{c}({n})" for c, n in m["code_top"]]))
        if m["model_top"]:
            out.append("主力模型: " + "，".join([f"{x}({n})" for x, n in m["model_top"][:3]]))
        if m["channel_top"]:
            out.append("主力渠道: " + "，".join([f"{x}({n})" for x, n in m["channel_top"][:3]]))

        out.append("\n🐢 最慢请求 Top5")
        for it in m["slow_items"][:5]:
            t = int(it.get("created_at", 0) or 0)
            mod = str(it.get("model_name") or "未知模型")
            code = int(it.get("code", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            pt = int(it.get("prompt_tokens", 0) or 0)
            ct = int(it.get("completion_tokens", 0) or 0)
            out.append(f"- {self._fmt_ts(t)} | {mod} | code={code} | {use}ms | token {pt}/{ct}")

        out.append("\n🧾 最近明细（新→旧）")
        for it in items[:20]:
            t = int(it.get("created_at", 0) or 0)
            mod = str(it.get("model_name") or "未知模型")
            typ = int(it.get("type", 0) or 0)
            code = int(it.get("code", 0) or 0)
            use = int(it.get("use_time", 0) or 0)
            pt = int(it.get("prompt_tokens", 0) or 0)
            ct = int(it.get("completion_tokens", 0) or 0)
            icon = "🔴" if typ == 5 or code >= 500 else ("🟠" if code >= 400 else "🟢")
            lat = "🐢" if use >= 15000 else ("⚠️" if use >= 5000 else "⚡")
            out.append(f"{icon} {self._fmt_ts(t)} | {mod} | code={code} | {lat}{use}ms | token {pt}/{ct}")

        if len(items) > 20:
            out.append(f"… 其余 {len(items)-20} 条已省略，可用 /日志 {min(100, len(items))} 查看更多")

        return "\n".join(out)

    def _detect_abnormal(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "🚨 异常分析\n暂无日志数据，无法判断。"

        m = self._summarize_log_metrics(items)
        errs = m["err_items"]
        total = m["total"]
        err_rate = m["err_rate"]
        slow_rate = m["slow15_rate"]

        lines = ["🚨 异常分析"]
        lines.append(
            f"总请求 {total} | 错误 {m['err_count']} ({err_rate:.1%}) | 超慢>=15s {m['slow15_count']} ({slow_rate:.1%})"
        )
        lines.append(f"耗时分位: p50 {m['p50_ms']}ms | p95 {m['p95_ms']}ms | p99 {m['p99_ms']}ms")

        if err_rate >= 0.2:
            lvl = "P0"
            reason = "错误率过高，已显著影响可用性"
        elif err_rate >= 0.08 or m["slow15_count"] >= 5:
            lvl = "P1"
            reason = "稳定性退化，建议尽快处理"
        elif err_rate > 0 or m["slow15_count"] > 0:
            lvl = "P2"
            reason = "存在零星异常，建议观察并优化"
        else:
            lvl = "OK"
            reason = "未发现明显异常"
        lines.append(f"风险等级: {lvl}（{reason}）")

        if m["code_top"]:
            lines.append("状态码分布: " + "，".join([f"{c}({n})" for c, n in m["code_top"]]))
        if m["model_top"]:
            lines.append("高风险模型候选: " + "，".join([f"{x}({n})" for x, n in m["model_top"][:3]]))
        if m["channel_top"]:
            lines.append("高风险渠道候选: " + "，".join([f"{x}({n})" for x, n in m["channel_top"][:3]]))

        if errs:
            lines.append("\n🧯 最近错误样本")
            for it in errs[:8]:
                t = int(it.get("created_at", 0) or 0)
                mod = str(it.get("model_name") or "未知模型")
                code = int(it.get("code", 0) or 0)
                use = int(it.get("use_time", 0) or 0)
                chan = str(it.get("channel_name") or it.get("channel") or it.get("channel_id") or "未知渠道")
                lines.append(f"- {self._fmt_ts(t)} | {mod} | {chan} | code={code} | {use}ms")

        lines.append("\n✅ 建议动作")
        if lvl in ("P0", "P1"):
            lines.append("1) 按状态码和渠道做分组，先切掉最差渠道验证")
            lines.append("2) 对高风险模型降并发并收敛 max_tokens")
            lines.append("3) 对 p95>阈值模型做专项回放排查")
        elif lvl == "P2":
            lines.append("1) 优先优化最慢 Top5 请求的参数与提示词长度")
            lines.append("2) 持续观测错误率/延迟曲线，防止抬头")
        else:
            lines.append("1) 当前健康，建议保留分模型/分渠道周报")

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
            "你是资深 NewAPI SRE 分析助手。请严格基于输入数据，输出面向运营排障的中文报告。\n"
            "用户关注点：24h vs 2h 消耗变化、模型与渠道集中度、错误原因、慢请求原因。\n"
            "禁止泛泛建议，不讨论余额与预算，不输出与数据无关内容。\n\n"
            "输出结构（必须按此顺序）：\n"
            "# 24h 与 2h 消耗结论\n"
            "- 24h 与 2h 的 token / 请求 / quota 对比（给出变化或占比）\n"
            "- 说明最近2小时是否异常放大或收缩\n\n"
            "# 模型与渠道集中度\n"
            "- Top 模型（24h、2h 各列）并解释主要负载模型\n"
            "- Top 渠道（24h、2h 各列）并解释集中在哪些渠道\n\n"
            "# 错误与慢请求根因\n"
            "- 按状态码分布解释主要报错类型\n"
            "- 结合模型/渠道/耗时样本，给出最可能根因（最多3条）\n\n"
            "# 处理建议（可执行）\n"
            "- 给出3~5条操作，优先能直接落地验证\n\n"
            "# 关键信号看板\n"
            "- 列出应持续观察的指标：错误率、p95、p99、超慢占比、Top模型/渠道变化\n\n"
            "如果输入缺少渠道或错误字段，明确写“数据缺失：xxx”，不要猜测。\n\n"
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
        # usage: 24h 与 2h 双窗口
        usage_24 = await self._fetch_usage_payload(24)
        usage_2 = await self._fetch_usage_payload(2)
        rec_24 = self._extract_records(usage_24)
        rec_2 = self._extract_records(usage_2)

        s24, e24 = self._window(24)
        s2, e2 = self._window(2)

        stats_24, models_24 = self._aggregate(rec_24, s24, e24)
        stats_2, models_2 = self._aggregate(rec_2, s2, e2)

        channels_24 = self._aggregate_by_keys(
            rec_24,
            s24,
            e24,
            ["channel_name", "channel", "channel_id", "provider_name", "provider", "provider_id"],
            "未知渠道",
        )
        channels_2 = self._aggregate_by_keys(
            rec_2,
            s2,
            e2,
            ["channel_name", "channel", "channel_id", "provider_name", "provider", "provider_id"],
            "未知渠道",
        )

        # logs: 24h 与 2h 双窗口（用于错误/耗时分析）
        logs_24 = await self._fetch_logs_payload(max(self.default_log_limit, 100), 24)
        logs_2 = await self._fetch_logs_payload(max(self.default_log_limit, 100), 2)
        log_items_24 = self._extract_log_items(logs_24)
        log_items_2 = self._extract_log_items(logs_2)

        m24 = self._summarize_log_metrics(log_items_24)
        m2 = self._summarize_log_metrics(log_items_2)

        brief = {
            "dual_window_usage": {
                "24h": {
                    "summary": stats_24,
                    "top_models": [
                        {"name": m, "token": s.get("token", 0), "req": s.get("count", 0), "quota": s.get("quota", 0)}
                        for m, s in models_24[:8]
                    ],
                    "top_channels": [
                        {"name": c, "token": s.get("token", 0), "req": s.get("count", 0), "quota": s.get("quota", 0)}
                        for c, s in channels_24[:8]
                    ],
                },
                "2h": {
                    "summary": stats_2,
                    "top_models": [
                        {"name": m, "token": s.get("token", 0), "req": s.get("count", 0), "quota": s.get("quota", 0)}
                        for m, s in models_2[:8]
                    ],
                    "top_channels": [
                        {"name": c, "token": s.get("token", 0), "req": s.get("count", 0), "quota": s.get("quota", 0)}
                        for c, s in channels_2[:8]
                    ],
                },
            },
            "dual_window_logs": {
                "24h": {
                    "summary": {
                        "total": m24["total"],
                        "err_count": m24["err_count"],
                        "err_rate": round(m24["err_rate"], 4),
                        "slow15_count": m24["slow15_count"],
                        "slow15_rate": round(m24["slow15_rate"], 4),
                        "avg_ms": m24["avg_ms"],
                        "p50_ms": m24["p50_ms"],
                        "p95_ms": m24["p95_ms"],
                        "p99_ms": m24["p99_ms"],
                    },
                    "code_dist": m24["code_top"],
                    "model_dist": m24["model_top"],
                    "channel_dist": m24["channel_top"],
                    "recent_errors": [
                        {
                            "time": self._fmt_ts(int(it.get("created_at", 0) or 0)),
                            "model": str(it.get("model_name") or "未知模型"),
                            "channel": str(it.get("channel_name") or it.get("channel") or it.get("channel_id") or "未知渠道"),
                            "code": int(it.get("code", 0) or 0),
                            "use_time_ms": int(it.get("use_time", 0) or 0),
                        }
                        for it in m24["err_items"][:12]
                    ],
                    "slow_top": [
                        {
                            "time": self._fmt_ts(int(it.get("created_at", 0) or 0)),
                            "model": str(it.get("model_name") or "未知模型"),
                            "channel": str(it.get("channel_name") or it.get("channel") or it.get("channel_id") or "未知渠道"),
                            "code": int(it.get("code", 0) or 0),
                            "use_time_ms": int(it.get("use_time", 0) or 0),
                        }
                        for it in m24["slow_items"][:8]
                    ],
                },
                "2h": {
                    "summary": {
                        "total": m2["total"],
                        "err_count": m2["err_count"],
                        "err_rate": round(m2["err_rate"], 4),
                        "slow15_count": m2["slow15_count"],
                        "slow15_rate": round(m2["slow15_rate"], 4),
                        "avg_ms": m2["avg_ms"],
                        "p50_ms": m2["p50_ms"],
                        "p95_ms": m2["p95_ms"],
                        "p99_ms": m2["p99_ms"],
                    },
                    "code_dist": m2["code_top"],
                    "model_dist": m2["model_top"],
                    "channel_dist": m2["channel_top"],
                    "recent_errors": [
                        {
                            "time": self._fmt_ts(int(it.get("created_at", 0) or 0)),
                            "model": str(it.get("model_name") or "未知模型"),
                            "channel": str(it.get("channel_name") or it.get("channel") or it.get("channel_id") or "未知渠道"),
                            "code": int(it.get("code", 0) or 0),
                            "use_time_ms": int(it.get("use_time", 0) or 0),
                        }
                        for it in m2["err_items"][:12]
                    ],
                    "slow_top": [
                        {
                            "time": self._fmt_ts(int(it.get("created_at", 0) or 0)),
                            "model": str(it.get("model_name") or "未知模型"),
                            "channel": str(it.get("channel_name") or it.get("channel") or it.get("channel_id") or "未知渠道"),
                            "code": int(it.get("code", 0) or 0),
                            "use_time_ms": int(it.get("use_time", 0) or 0),
                        }
                        for it in m2["slow_items"][:8]
                    ],
                },
            },
        }

        preface = self._format_dual_window_report(
            stats_24, models_24, channels_24,
            stats_2, models_2, channels_2,
            m24["channel_top"], m2["channel_top"],
        )
        llm_text = await self._llm_analyze(event, "24h/2h 消耗与日志分析", json.dumps(brief, ensure_ascii=False))
        text = preface + "\n\n" + llm_text
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
        out.append(f"plugin_version: 2.3.1")
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
