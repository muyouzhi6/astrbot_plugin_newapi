## astrbot_plugin_newapi
从可配置的上游 API 获取账户用量并生成统计报告，支持查询最近 API 调用日志与当前用户信息，可按需以合并转发或纯文本展示。

### 功能特性
- **用量统计报告**: 固定时间窗聚合（默认 1500 分钟 = 25 小时），输出总使用量、总请求数、总配额、平均 RPM/TPM，并支持展示调用最多的 Top N 模型。
- **调用日志查询**: 快速拉取最近一段时间的调用日志（默认 24 小时，最多 20 条），支持合并转发或文本分段发送。
- **用户信息查询**: 查询当前用户的额度与基本信息（/api/user/self）。
- **健壮的回退策略**: 远端返回异常或空数据时，自动回退读取本地 `data.json`。
- **隐私与日志**: 自动掩码敏感字段（如 Authorization、New-Api-User、IP）以便于调试且避免泄露。

### 兼容与前置条件
- 需要在 AstrBot 环境中使用（本插件基于 AstrBot 插件 API 开发）。
- 上游接口需要提供以下端点（或语义兼容的返回结构）：
  - `/api/data/self`（用量聚合数据）
  - `/api/log/`（调用日志）
  - `/api/user/self`（用户信息）

### 安装
1) 从源码安装
- 将本仓库克隆/下载至 AstrBot 的插件目录，例如：`AstrBot/plugins/newapi`。
- 目录中需包含：`main.py`、`_conf_schema.json`、`metadata.yaml`。

2) 从插件市场安装
- 若已上架至 AstrBot 插件市场，可通过市场一键安装（以实际上架情况为准）。

### 配置
通过 UI 或直接编辑 `_conf_schema.json` 对应项完成配置。关键字段：

- **base_domain**: 上游接口域名（含协议），如 `https://new.xigua.wiki`。
- **authorization**: HTTP Header 中的 `Authorization` 值。
- **new_api_user**: HTTP Header 中的 `New-Api-User` 值。
  ![e5e7b5a0fb6e0a3ce57b33eb64017a3e_720](https://github.com/user-attachments/assets/a82ff0eb-c2fd-4119-b4be-3703a9856b81)

- **request_timeout**: 请求超时（秒），默认 15。
- **use_forward**: 是否使用合并转发发送“用量统计”报告，默认开启。
- **log_use_forward**: 查询日志是否使用合并转发，默认开启。
- **user_use_forward**: 查询用户信息是否使用合并转发，默认关闭。
- **show_top_models**: 是否显示调用最多的模型（Top N），默认开启。
- **top_n_models**: Top N 的数量，默认 3。
- **log_page_size**: 日志查询条数，默认 20。

示例（仅示意）：
```json
{
  "base_domain": "https://new.xigua.wiki",
  "authorization": "Bearer xxx",
  "new_api_user": "12345",
  "use_forward": true,
  "show_top_models": true,
  "top_n_models": 3,
  "log_page_size": 20
}
```

### 使用方式（指令）
- **/tokens统计**
  - 说明：以“当前时间+1 小时”为结束时间，向前回溯固定 1500 分钟统计（可在代码中改默认，当前版本固定为 1500）。
  - 输出：总使用量 tokens、总请求数、总配额、平均 RPM/TPM，以及 Top N 模型明细（可配置）。
  - 展示：优先使用合并转发（`use_forward=true`），否则自动分段纯文本发送。

- **/logs**
  - 说明：查询最近 24 小时内的调用日志（默认分页大小 `log_page_size=20`，type=0）。
  - 展示：受 `log_use_forward` 控制，开启则以合并转发发送，否则按文本切片分多条发送。

- **/查询额度**
  - 说明：调用 `/api/user/self`，展示用户名、昵称、分组、请求次数、已用配额、当前额度（配额/500）。
  - 展示：受 `user_use_forward` 控制。

### 输出示例
用量统计（片段）：
```
--- 数据分析报告 ---
计算时间跨度: 1500 分钟
数据范围: 2025-01-01 00:00:00 CST+8 至 2025-01-02 01:00:00 CST+8
总使用量 (tokens): 12,345
总请求次数: 678
总配额: 90,000
平均 RPM: 0.452
平均 TPM: 8.230
-------------------------
调用最多的前 3 个模型：
...
```

### 数据格式与统计口径
- 时间窗口：固定分钟数回溯（默认 1500 分钟），平均值按该分钟数计算。
- 记录字段假设：
  - `created_at`（时间戳，秒）
  - `model_name`、`token_used`、`count`、`quota`
- 若上游返回结构不同，插件会尝试在常见路径中查找列表：`data`、`data.data`、`data.list`、`list` 等。

### 故障排查
- **报错/无数据**：
  - 检查 `base_domain` 是否正确，且接口可访问。
  - 确认 `authorization` / `new_api_user` 是否填写且有效。
  - 注意服务器时间与时区，时间窗使用 UTC 与 CST+8 混合日志用于可读性。
- **超时**：适当增大 `request_timeout`。
- **远端非 JSON 返回**：日志会提示并回退为 `{ error: non_json_response }`；可检查接口。
- **结果异常**：插件会将最近一次成功响应写入本地 `data.json`，便于排查与离线统计。

### 安全与隐私
- 日志会对 `Authorization`、`New-Api-User` 等敏感信息进行掩码。
- 发送日志中会对 IP 地址进行简化显示（如 `1.2.x.x`）。

### 版本信息
- 当前版本：`1.0.0`
- 作者：枫
- 仓库：`https://github.com/15515151/astrbot_plugin_newapi`

### 免责声明
本插件仅用于对接并展示上游 API 的统计信息。请确保你拥有合法的 API 访问权限，并遵循相关服务条款与当地法律法规。
