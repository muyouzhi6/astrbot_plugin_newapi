## astrbot_plugin_newapi v2（简指令 + 运维分析）

本版本目标：**中文、短指令、低学习成本**。

### 新指令（简体中文）
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
2. 统计逻辑修复（去掉旧版重复输出）
3. 增加异常检测（错误数、慢请求）
4. 增加可选 LLM 分析（报错与调用情况）
5. 元数据对齐 AstrBot 新规范（`astrbot_version`、`support_platforms`）
6. 存储路径优先使用 AstrBot `plugin_data/{plugin_name}` 规范

### 配置项（新增）
- `default_window_hours`
- `default_top_n`
- `llm_enabled`
- `llm_base_url`
- `llm_api_key`
- `llm_model`
- `llm_timeout`

### LLM 分析说明
开启 `llm_enabled=true` 后：
- `/分析`：基于统计+异常生成运维简报
- `/建议`：给出优化动作（P0/P1）

### 注意
- LLM 分析使用 OpenAI 兼容接口：`{llm_base_url}/chat/completions`
- 若未配置 LLM，将提示“未开启/配置不完整”
