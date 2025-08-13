import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "newapi",
    "枫",
    "从可配置的API 拉取用量数据，按固定时间跨度统计 RPM/TPM/TopN 模型，并在聊天中返回报告",
    "1.0.0",
)
class XiguaUsageReporter(Star):
    """
    一个 AstrBot 插件：
    - 通过可配置的 `base_url`、`Authorization`、`New-Api-User` 请求上游 API
    - 使用固定的时间跨度（分钟）对数据进行聚合计算
    - 输出总使用量、总请求数、总配额、平均 RPM/TPM，以及使用量 Top N 的模型
    - 可选将原始 JSON 响应保存到插件目录下的 `data.json`
    - 固定使用配置中的 `time_span_minutes`（默认 1500 分钟 = 25 小时）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 本插件目录与数据文件路径
        self._plugin_dir: Path = Path(__file__).resolve().parent
        self.data_file_path: Path = self._plugin_dir / "data.json"
        # 基础请求配置（仅域名 + 可配置路径）
        self.base_domain: str = (
            config.get("base_domain")
            or config.get("base_url")  # 兼容旧字段
            or "https://new.xigua.wiki"
        ).strip()
        # 接口路径固定为本插件的默认值，配置文件不再提供路径项
        self.endpoint_path: str = "/api/data/self"
        self.authorization: str = config.get("authorization", "").strip()
        self.new_api_user: str = config.get("new_api_user", "").strip()
        self.request_timeout: int = int(config.get("request_timeout", 15))

        # 统计与展示配置
        self.time_span_minutes_default: int = 1500
        self.show_top_models: bool = bool(config.get("show_top_models", True))
        try:
            self.top_n_models: int = int(config.get("top_n_models", 3))
        except Exception:
            self.top_n_models = 3
        self.save_raw_json: bool = True
        # 是否使用合并转发发送（允许通过配置开关）
        try:
            self.use_forward: bool = bool(config.get("use_forward", True))
        except Exception:
            self.use_forward = True
        self.log_verbose: bool = True
        self.max_log_body_chars: int = 500
        # 记录最近一次构造的时间窗，便于日志核对
        self._last_start_ts: int = 0
        self._last_end_ts: int = 0

        # 日志查询配置
        try:
            self.log_page_size: int = int(config.get("log_page_size", 20))
        except Exception:
            self.log_page_size = 20
        try:
            self.log_use_forward: bool = bool(config.get("log_use_forward", self.use_forward))
        except Exception:
            self.log_use_forward = self.use_forward
        try:
            self.user_use_forward: bool = bool(config.get("user_use_forward", False))
        except Exception:
            self.user_use_forward = False

        logger.info(
            f"已加载 [XiguaUsageReporter] v1.0.0，默认统计 {self.time_span_minutes_default} 分钟，Top{self.top_n_models} 模型。"
        )

    async def _http_get_json(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """使用标准库发起 GET 请求并解析 JSON，避免额外依赖。"""
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError

        if self.log_verbose:
            masked_headers = dict(headers)
            if "Authorization" in masked_headers:
                masked_headers["Authorization"] = self._mask_secret(masked_headers["Authorization"])
            if "New-Api-User" in masked_headers:
                masked_headers["New-Api-User"] = self._mask_secret(str(masked_headers["New-Api-User"]))
            logger.debug(f"HTTP GET 即将请求: url={url}, headers={masked_headers}")

        req = Request(url=url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)

        def _do() -> Dict[str, Any]:
            with urlopen(req, timeout=self.request_timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = -1
                ct = None
                try:
                    ct = resp.headers.get("Content-Type")
                except Exception:
                    ct = None
                data = resp.read()
                body_len = len(data) if data else 0
                if self.log_verbose:
                    logger.debug(f"HTTP 响应: status={status}, content_type={ct}, body_len={body_len}")
                # 尝试解析 JSON
                text = None
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    pass
                try:
                    return json.loads(text if text is not None else data)
                except Exception as e:
                    if self.log_verbose:
                        snippet = (text or "")[: self.max_log_body_chars]
                        logger.debug(f"HTTP 响应非 JSON，解析失败: {e}; 片段: {snippet}")
                    return {"error": "non_json_response", "status": status, "content_type": ct}

        try:
            result = await asyncio.to_thread(_do)
            if self.log_verbose:
                if isinstance(result, dict):
                    logger.debug(f"HTTP 响应已解析为 JSON 对象，顶层键: {list(result.keys())[:20]}")
                elif isinstance(result, list):
                    logger.debug(f"HTTP 响应已解析为 JSON 列表，长度: {len(result)}")
                else:
                    logger.debug(f"HTTP 响应已解析为 JSON，类型: {type(result).__name__}")
            return result
        except HTTPError as e:
            text = f"HTTP {e.code} {e.reason}"
            logger.error(f"请求失败: {text}")
            return {"error": text}
        except URLError as e:
            text = f"URL 错误: {e.reason}"
            logger.error(f"请求失败: {text}")
            return {"error": text}
        except Exception as e:
            text = f"请求异常: {e}"
            logger.error(text)
            return {"error": text}

    def _extract_records(self, payload: Any) -> List[Dict[str, Any]]:
        """更宽松地提取记录列表，兼容多种返回格式，并输出详细日志。"""
        try:
            ptype = type(payload).__name__
            if self.log_verbose:
                logger.debug(f"extract_records: 顶层类型={ptype}")
            # 直接是列表
            if isinstance(payload, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用顶层列表，len={len(payload)}")
                return payload  # type: ignore
            if not isinstance(payload, dict):
                return []
            # 常见：data 为列表
            data = payload.get("data")
            if isinstance(data, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用 data(list)，len={len(data)}")
                return data  # type: ignore
            # data 为对象，其中再包含 data/list
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, list):
                    if self.log_verbose:
                        logger.debug(f"extract_records: 使用 data.data(list)，len={len(inner)}")
                    return inner  # type: ignore
                inner = data.get("list")
                if isinstance(inner, list):
                    if self.log_verbose:
                        logger.debug(f"extract_records: 使用 data.list(list)，len={len(inner)}")
                    return inner  # type: ignore
            # 顶层 list
            lst = payload.get("list")
            if isinstance(lst, list):
                if self.log_verbose:
                    logger.debug(f"extract_records: 使用 list(list)，len={len(lst)}")
                return lst  # type: ignore
            if self.log_verbose:
                logger.debug("extract_records: 未在常见路径发现列表，返回空")
            return []
        except Exception as e:
            logger.warning(f"extract_records: 解析异常: {e}")
            return []

    def _analyze(self, records: List[Dict[str, Any]], start_timestamp: int, end_timestamp: int, time_span_minutes: int) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, int]]]]:
        """使用当前时刻回溯的固定窗口 [start_timestamp, end_timestamp] 进行统计；平均值以 time_span_minutes 为分母。"""
        if start_timestamp <= 0 or end_timestamp <= 0 or end_timestamp < start_timestamp:
            end_timestamp = int(time.time())
            start_timestamp = end_timestamp - (time_span_minutes * 60)

        total_tokens_used = 0
        total_requests = 0
        total_quota = 0

        model_stats: Dict[str, Dict[str, int]] = {}

        for r in records:
            created_at = int(r.get("created_at", 0) or 0)
            if start_timestamp <= created_at <= end_timestamp:
                model_name = r.get("model_name")
                tokens_used = int(r.get("token_used", 0) or 0)
                count = int(r.get("count", 0) or 0)
                quota = int(r.get("quota", 0) or 0)

                total_tokens_used += tokens_used
                total_requests += count
                total_quota += quota

                if model_name:
                    entry = model_stats.setdefault(model_name, {"total_tokens": 0, "total_requests": 0, "total_quota": 0})
                    entry["total_tokens"] += tokens_used
                    entry["total_requests"] += count
                    entry["total_quota"] += quota

        minutes_for_avg = max(1, int(time_span_minutes))
        avg_rpm = (total_requests / minutes_for_avg) if minutes_for_avg > 0 else 0.0
        avg_tpm = (total_tokens_used / minutes_for_avg) if minutes_for_avg > 0 else 0.0

        stats = {
            "time_span_minutes": time_span_minutes,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "total_tokens_used": total_tokens_used,
            "total_requests": total_requests,
            "total_quota": total_quota,
            "avg_rpm": avg_rpm,
            "avg_tpm": avg_tpm,
        }

        # 调用最多（按请求次数）排序
        sorted_models = sorted(model_stats.items(), key=lambda kv: kv[1]["total_requests"], reverse=True)
        return stats, sorted_models

    @staticmethod
    def _fmt_ts(ts: int) -> str:
        if not ts:
            return "-"
        try:
            tz = timezone(timedelta(hours=8), name="CST+8")
            return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return str(ts)

    def _format_report(self, stats: Dict[str, Any], sorted_models: List[Tuple[str, Dict[str, int]]]) -> str:
        start_time_str = self._fmt_ts(int(stats.get("start_timestamp", 0)))
        end_time_str = self._fmt_ts(int(stats.get("end_timestamp", 0)))
        span_minutes = float(stats.get("time_span_minutes", 0.0) or 0.0)
        lines = [
            "--- 数据分析报告 ---",
            f"计算时间跨度: {int(span_minutes)} 分钟",
            f"数据范围: {start_time_str} 至 {end_time_str}",
            f"总使用量 (tokens): {stats.get('total_tokens_used', 0):,}",
            f"总请求次数: {stats.get('total_requests', 0):,}",
            f"总配额: {stats.get('total_quota', 0):,}",
            f"平均 RPM: {float(stats.get('avg_rpm', 0.0)):.3f}",
            f"平均 TPM: {float(stats.get('avg_tpm', 0.0)):.3f}",
            "-------------------------",
        ]

        if self.show_top_models and self.top_n_models > 0 and sorted_models:
            lines.append(f"调用最多的前 {self.top_n_models} 个模型：")
            span_minutes_float = max(1e-9, float(stats.get("time_span_minutes", 0.0) or 0.0))
            for model, s in sorted_models[: self.top_n_models]:
                avg_tpm_model = (s["total_tokens"] / span_minutes_float) if span_minutes_float > 0 else 0.0
                avg_rpm_model = (s["total_requests"] / span_minutes_float) if span_minutes_float > 0 else 0.0
                lines.append("")
                lines.append(f"模型: {model}")
                lines.append(f"  - Token总和: {s['total_tokens']:,}")
                lines.append(f"  - 请求总数: {s['total_requests']:,}")
                lines.append(f"  - 平均 TPM: {avg_tpm_model:.3f}")
                lines.append(f"  - 平均 RPM: {avg_rpm_model:.3f}")
                lines.append(f"  - 配额: {s['total_quota']:,}")
            lines.append("")
            lines.append(f"模型: {model}")
            lines.append(f"  - Token总和: {s['total_tokens']:,}")
            lines.append(f"  - 请求总数: {s['total_requests']:,}")
            lines.append(f"  - 平均 TPM: {avg_tpm_model:.3f}")
            lines.append(f"  - 平均 RPM: {avg_rpm_model:.3f}")
            lines.append(f"  - 配额: {s['total_quota']:,}")

        return "\n".join(lines)

    @staticmethod
    def _mask_secret(value: str, left: int = 4, right: int = 2) -> str:
        try:
            v = str(value)
            if len(v) <= left + right:
                return "*" * len(v)
            return v[:left] + "..." + v[-right:]
        except Exception:
            return "***"

    def _build_forward_node(self, text: str) -> Any:
        """将文本包装为合并转发 Node。"""
        try:
            conf_uin = getattr(self, "forward_uin", None)
            if conf_uin is None and hasattr(self, "config"):
                conf_uin = self.config.get("forward_uin")  # type: ignore
            forward_uin = int(conf_uin) if conf_uin not in (None, "", 0) else 10000
        except Exception:
            forward_uin = 10000
        forward_name = getattr(self, "forward_name", None) or (
            getattr(self, "config", {}).get("forward_name") if hasattr(self, "config") else None  # type: ignore
        ) or "xuxue07 Bot"
        return Comp.Node(
            uin=forward_uin,
            name=forward_name,
            content=[Comp.Plain(text)],
        )

    def _build_forward_nodes(self, text: str) -> List[Any]:
        """将长文本切分为多段，生成多个 Node。"""
        max_len = 900
        parts: List[str] = []
        t = text or ""
        while t:
            parts.append(t[:max_len])
            t = t[max_len:]
        if not parts:
            parts = ["(空)"]
        nodes = [self._build_forward_node(p) for p in parts]
        return nodes

    async def _save_raw_json(self, payload: Dict[str, Any]):
        if not self.save_raw_json:
            return
        try:
            with open(self.data_file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            if self.log_verbose:
                try:
                    size = self.data_file_path.stat().st_size
                    logger.debug(f"已保存原始 JSON 到 {self.data_file_path} (size={size} bytes)")
                except Exception:
                    logger.debug(f"已保存原始 JSON 到 {self.data_file_path}")
        except Exception as e:
            logger.warning(f"保存 data.json 失败: {e}")

    async def _load_local_json(self) -> Dict[str, Any]:
        try:
            if self.log_verbose:
                logger.debug(f"尝试从本地读取: {self.data_file_path} (exists={self.data_file_path.exists()})")
            with open(self.data_file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"读取 data.json 失败: {e}")
            return {}

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
        }
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.new_api_user:
            headers["New-Api-User"] = self.new_api_user
        return headers

    def _build_url(self, minutes: int) -> str:
        path = self.endpoint_path or "/api/data/self"
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_domain.rstrip("/") + path

        # 计算时间窗口 - 使用当前时间作为结束时间
        end_ts = int(time.time())
        start_ts = end_ts - minutes * 60
        self._last_start_ts = start_ts
        self._last_end_ts = end_ts

        # 追加开始/结束时间戳与默认粒度（固定为 username=''，default_time='hour'）
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            q.update({
                "username": "",
                "start_timestamp": str(start_ts),
                "end_timestamp": str(end_ts),
                "default_time": "hour",
            })
            url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"构造时间戳查询参数失败: {e}")
        
        if self.log_verbose:
            # 附带可读时间窗口
            try:
                cst_tz = timezone(timedelta(hours=8))
                def fmt(ts: int) -> str:
                    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    cst = datetime.fromtimestamp(ts, cst_tz).strftime("%Y-%m-%d %H:%M:%S CST+8")
                    return f"{utc} | {cst}"
                win = f"start={start_ts}({fmt(start_ts)}) -> end={end_ts}({fmt(end_ts)})"
            except Exception:
                win = f"start={start_ts} -> end={end_ts}"
            logger.debug(
                f"构造 URL: domain={self.base_domain}, path={path}, url={url}, minutes={minutes}, window={win}"
            )
        return url

    def _build_url_with_range(self, start_ts: int, end_ts: int) -> str:
        path = self.endpoint_path or "/api/data/self"
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_domain.rstrip("/") + path

        self._last_start_ts = int(start_ts)
        self._last_end_ts = int(end_ts)

        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            q.update({
                "username": "",
                "start_timestamp": str(self._last_start_ts),
                "end_timestamp": str(self._last_end_ts),
                "default_time": "hour",
            })
            url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"构造时间戳查询参数失败: {e}")

        if self.log_verbose:
            try:
                cst_tz = timezone(timedelta(hours=8))
                def fmt(ts: int) -> str:
                    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    cst = datetime.fromtimestamp(ts, cst_tz).strftime("%Y-%m-%d %H:%M:%S CST+8")
                    return f"{utc} | {cst}"
                win = f"start={self._last_start_ts}({fmt(self._last_start_ts)}) -> end={self._last_end_ts}({fmt(self._last_end_ts)})"
            except Exception:
                win = f"start={self._last_start_ts} -> end={self._last_end_ts}"
            logger.debug(
                f"构造 URL(指定范围): domain={self.base_domain}, path={path}, url={url}, window={win}"
            )
        return url

    def _build_log_headers(self) -> Dict[str, str]:
        # 与获取用量相同的鉴权逻辑
        return self._build_headers()

    def _build_log_url(self, params: Dict[str, Any]) -> str:
        base = self.base_domain.rstrip("/") + "/api/log/"
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(base)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            # 合并传入查询参数
            for k, v in (params or {}).items():
                q[str(k)] = str(v)
            return urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
        except Exception:
            # 简单拼接
            try:
                from urllib.parse import urlencode
                return base.rstrip("?") + ("?" + urlencode(params or {}))
            except Exception:
                return base

    async def _fetch_logs(self, params: Dict[str, Any]) -> Any:
        url = self._build_log_url(params)
        headers = self._build_log_headers()
        return await self._http_get_json(url, headers)

    @staticmethod
    def _mask_ip(ip: Any) -> str:
        try:
            s = str(ip or "")
            if not s:
                return "无IP信息"
            parts = s.split(".")
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}.x.x"
            return s
        except Exception:
            return "无IP信息"

    @staticmethod
    def _format_log_type(t: Any) -> str:
        try:
            iv = int(t)
            if iv == 2:
                return "消费"
            if iv == 5:
                return "错误"
            return "其他"
        except Exception:
            return "其他"

    def _extract_log_items(self, payload: Any) -> List[Dict[str, Any]]:
        try:
            if isinstance(payload, list):
                return payload  # type: ignore
            if not isinstance(payload, dict):
                return []
            data = payload.get("data")
            if isinstance(data, dict):
                items = data.get("items") or data.get("list") or []
                if isinstance(items, list):
                    return items  # type: ignore
            # 顶层 items/list
            items = payload.get("items") or payload.get("list")
            if isinstance(items, list):
                return items  # type: ignore
            return []
        except Exception:
            return []

    def _format_log_item(self, item: Dict[str, Any]) -> str:
        created_at = 0
        try:
            created_at = int(item.get("created_at", 0) or 0)
        except Exception:
            created_at = 0
        log_time = self._fmt_ts(created_at)
        log_type = self._format_log_type(item.get("type"))
        model = item.get("model_name") or "未知模型"
        prompt_tokens = int(item.get("prompt_tokens", 0) or 0)
        completion_tokens = int(item.get("completion_tokens", 0) or 0)
        use_time = int(item.get("use_time", 0) or 0)
        ip_masked = self._mask_ip(item.get("ip"))
        lines = [
            f"🕒 {log_time}",
            f"📌 {log_type}",
            f"🤖 {model}",
            f"📥 输入: {prompt_tokens}",
            f"📤 输出: {completion_tokens}",
            f"⏱️ 耗时: {use_time}ms",
            f"🌐 IP: {ip_masked}",
        ]
        return "\n ".join(lines)

    def _build_user_self_url(self) -> str:
        return self.base_domain.rstrip("/") + "/api/user/self"

    async def _fetch_user_self(self) -> Any:
        url = self._build_user_self_url()
        headers = self._build_headers()
        return await self._http_get_json(url, headers)

    def _format_user_self(self, payload: Any) -> str:
        data: Dict[str, Any] = {}
        if isinstance(payload, dict):
            maybe = payload.get("data")
            if isinstance(maybe, dict):
                data = maybe
        username = str(data.get("username") or "-")
        display_name = str(data.get("display_name") or "-")
        group = str(data.get("group") or "-")
        role = int(data.get("role", 0) or 0)
        status = int(data.get("status", 0) or 0)
        request_count = int(data.get("request_count", 0) or 0)
        used_quota = int(data.get("used_quota", 0) or 0)
        quota = int(data.get("quota", 0) or 0)
        current_quota = quota / 500 if quota else 0
        access_token = self._mask_secret(str(data.get("access_token") or ""))
        lines = [
            "--- 用户信息 ---",
            f"用户名: {username}",
            f"昵称: {display_name}",
            f"分组: {group}",
            f"请求次数: {request_count:,}",
            f"已用配额: {used_quota:,}",
            f"当前额度(配额/500):$ {current_quota:,}",
        ]
        return "\n".join(lines)

    async def _fetch_payload(self, minutes: int, headers: Dict[str, str], start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> Any:
        """获取 payload：优先使用给定的 [start_ts, end_ts]；若无则按 minutes；若为空则回退用最新记录重拉。"""
        # 第一次：优先使用显式时间窗
        if start_ts is not None and end_ts is not None:
            url = self._build_url_with_range(int(start_ts), int(end_ts))
        else:
            url = self._build_url(minutes)
        payload = await self._http_get_json(url, headers)
        records = self._extract_records(payload)
        if records:
            return payload
        # 回退：不带时间窗获取一次，尝试拿到最新 created_at
        try:
            from urllib.parse import urlsplit, urlunsplit
            split = urlsplit(url)
            url_no_query = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
        except Exception:
            url_no_query = url
        probe = await self._http_get_json(url_no_query, headers)
        probe_records = self._extract_records(probe)
        if not probe_records:
            return payload
        try:
            latest = max(int(r.get("created_at", 0) or 0) for r in probe_records)
            if latest <= 0:
                return payload
            # 用最新记录时间作为 end，重新拉取
            self._last_end_ts = latest
            self._last_start_ts = latest - minutes * 60
            # 基于 latest 构造 URL
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            split = urlsplit(url_no_query)
            q = dict(parse_qsl(split.query, keep_blank_values=True))
            q.update({
                "username": "",
                "start_timestamp": str(self._last_start_ts),
                "end_timestamp": str(self._last_end_ts),
                "default_time": "hour",
            })
            url2 = urlunsplit((split.scheme, split.netloc, split.path, urlencode(q), split.fragment))
            if self.log_verbose:
                logger.debug(f"回退：基于最新记录 created_at={latest} 重构 URL 再次请求: {url2}")
            payload2 = await self._http_get_json(url2, headers)
            return payload2
        except Exception as e:
            if self.log_verbose:
                logger.debug(f"回退重拉失败: {e}")
            return payload

    @filter.command("tokens统计")
    async def handle_xigua_command(self, event: AstrMessageEvent):
        """命令：/tokens统计（固定 25 小时，或按配置 time_span_minutes）"""
        minutes = self.time_span_minutes_default
        # 实时窗口：以当前时刻+1小时为 end，向前回溯 minutes 分钟
        end_ts = int(time.time()) + 3600
        start_ts = end_ts - minutes * 60

        if not self.base_domain:
            text = "配置缺少 base_domain（仅域名，例如 https://new.xigua.wiki），请在 _conf_schema.json 中填写。"
            yield event.plain_result(text)
            return

        headers = self._build_headers()
        payload = await self._fetch_payload(minutes, headers, start_ts=start_ts, end_ts=end_ts)
        if self.log_verbose and isinstance(payload, dict):
            logger.debug(f"远端 payload 字段: keys={list(payload.keys())[:20]}, success={payload.get('success')}, message={payload.get('message')!r}")

        # 解析远端记录
        payload_records = self._extract_records(payload)

        payload_error = None
        if isinstance(payload, dict) and payload.get("error"):
            payload_error = str(payload.get("error"))
            logger.warning(f"远端请求失败，将回退读取本地 data.json：{payload_error}")
        else:
            # 默认先保存到本地（仅在解析为 JSON 时有效）
            if isinstance(payload, (dict, list)):
                await self._save_raw_json(payload)
            elif self.log_verbose:
                logger.debug("远端响应非 JSON，跳过落盘，仅使用本地 data.json 回退")

        # 读取本地 data.json
        local_payload = await self._load_local_json()
        if self.log_verbose:
            if isinstance(local_payload, dict):
                logger.debug(f"本地 JSON 顶层键: {list(local_payload.keys())[:20]}")
            elif isinstance(local_payload, list):
                logger.debug(f"本地 JSON 顶层为列表，长度: {len(local_payload)}")
        local_records = self._extract_records(local_payload)
        if self.log_verbose:
            logger.debug(f"本地记录数量: {len(local_records)}; 远端记录数量: {len(payload_records)}")
            if not local_records and isinstance(local_payload, dict):
                logger.debug(f"本地 JSON data/list 为空，success={local_payload.get('success')}, message={local_payload.get('message')!r}")

        # 优先使用本次请求的最新记录，若无则回退本地
        records = payload_records if payload_records else local_records
        stats, sorted_models = self._analyze(records, start_ts, end_ts, minutes)
        if self.log_verbose:
            logger.debug(
                f"统计: tokens={stats.get('total_tokens_used')}, requests={stats.get('total_requests')}, "
                f"quota={stats.get('total_quota')}, avg_rpm={stats.get('avg_rpm')}, avg_tpm={stats.get('avg_tpm')}"
            )

        report = self._format_report(stats, sorted_models)
        # 若无数据，给出醒目提示
        if not records:
            report = "[提示] 获取的数据为空\n" + report

        if self.use_forward:
            try:
                nodes = self._build_forward_nodes(report)
                # 简单校验 forward_uin
                if nodes and getattr(nodes[0], "uin", None):
                    yield event.chain_result(nodes)
                    return
            except Exception:
                pass
        # 纯文本模式：为避免单条过长发送失败，切片分多条发送
        max_len = 900
        text = report or ""
        if not text:
            yield event.plain_result("(空)")
            return
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)


    @filter.command("logs")
    async def handle_query_logs_en(self, event: AstrMessageEvent):
        async for result in self._handle_query_logs(event):
            yield result

    async def _handle_query_logs(self, event: AstrMessageEvent):
        # 默认：最近 24 小时、第一页、20 条、type=0
        end_ts = int(time.time())
        start_ts = end_ts - 86400
        params = {
            "p": 1,
            "page_size": self.log_page_size,
            "type": 0,
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
        }
        yield event.plain_result("正在查询最近的20条日志，请稍候...")
        payload = await self._fetch_logs(params)
        items = self._extract_log_items(payload)
        if not items:
            yield event.plain_result("未获取到有效日志数据")
            return
        # 构造合并转发（将所有日志合并到单个合并转发消息中）
        title = "📊 最近20条API调用日志"
        texts: List[str] = [self._format_log_item(it) for it in items]
        combined = "\n\n".join([title] + texts + [f"✅ 共查询到 {len(items)} 条日志"])
        if self.log_use_forward:
            try:
                nodes: List[Any] = [self._build_forward_node(combined)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        # 回退为纯文本多段发送
        max_len = 900
        text = combined or "(空)"
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    @filter.command("查询额度")
    async def handle_user_self(self, event: AstrMessageEvent):
        """命令：/查询额度 查询用户信息（/api/user/self）"""
        payload = await self._fetch_user_self()
        # 记录成功/失败日志
        if isinstance(payload, dict) and payload.get("success") is True:
            text = self._format_user_self(payload)
        else:
            # 兜底显示原始错误
            text = "[提示] 获取用户信息失败\n" + json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
        # 发送方式：根据 user_use_forward 决定
        if self.user_use_forward:
            try:
                nodes = [self._build_forward_node(text)]
                yield event.chain_result(nodes)
                return
            except Exception:
                pass
        # 纯文本发送
        max_len = 900
        while text:
            chunk = text[:max_len]
            text = text[max_len:]
            yield event.plain_result(chunk)

    async def terminate(self):
        logger.info("已卸载 [XiguaUsageReporter] 插件。")