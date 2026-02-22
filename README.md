## astrbot_plugin_newapi v2.1（简指令 + 运维分析）

本版本目标：**中文、短指令、低学习成本**。

### 新指令（简体中文）
- `/newapi`：查看指令帮助
- `/概览 [小时]`：默认24小时总览
- `/模型 [topN]`：模型排行
- `/日志 [条数]`：最近日志
- `/额度`：当前账户额度
- `/异常`：自动识别错误/慢请求
- `/分析`：LLM 分析调用情况
- `/建议`：LLM 生成优化建议
- `/健康`：连通性和配置检查

兼容旧指令：
- `/tokens统计` -> `/概览`
- `/logs` -> `/日志`
- `/查询额度` -> `/额度`

### 关键改进
1. 指令全面中文化 + 参数简化（默认值合理）
2. 增加 `/newapi` 帮助入口，避免“指令没注册”的体感
3. 统计逻辑修复（去掉旧版重复输出）
4. 增加异常检测（错误数、慢请求）
5. 增加可选 LLM 分析（报错与调用情况）
6. 元数据对齐 AstrBot 新规范（`astrbot_version`、`support_platforms`）
7. 存储路径优先使用 AstrBot `plugin_data/{plugin_name}` 规范

### LLM 服务商策略（按你的要求）
- 默认：使用**当前会话服务商**（`llm_use_current_provider=true`）
- 可选：关闭上面开关后，手动选择 `llm_provider_id`
- `llm_provider_id` 支持 `_special: select_provider` 下拉选择 AstrBot 已配置服务商

### 配置项（LLM相关）
- `llm_enabled`
- `llm_use_current_provider`
- `llm_provider_id`

### 注意
- 若启用 LLM 但无可用 provider，会在 `/分析` `/建议` 返回明确提示
