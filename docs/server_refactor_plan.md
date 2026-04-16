# server.py 瘦身计划（第一阶段）

## 1) `server.py` 应该负责什么
- 启动与生命周期：HTTP server 启停、基础配置。
- 路由分发：把请求分发给各业务模块。
- 通用基础设施：CORS、请求体解析、统一响应格式、日志入口。

## 2) `server.py` 不应该继续膨胀的部分
- 领域业务规则（例如：经络图通脉/开穴细节、反馈状态机）。
- 请求参数校验细节（应下沉到领域 API handler）。
- 数据访问细节（读写 JSON/DB 的具体 SQL/文件操作）。
- 长流程编排（应在 service 层做）。

## 3) 拆分类别（按优先级）
1. API Handler 层（参数校验 + 入参标准化）
2. Service 层（业务编排）
3. Storage/Repo 层（持久化访问）
4. Domain 层（纯规则/状态机）

## 4) 第一阶段已完成
- 新增 `dashboard/api/meridian_api.py`
  - 抽离经络图 4 个 POST 路由参数校验与调用编排。
- 新增 `dashboard/api/secretary_api.py`
  - 抽离秘书模块 2 个 POST 路由参数校验与调用编排。
- `server.py` 改为调用上述 API handler，减少路由函数体复杂度。
- 新建 `dashboard/api/__init__.py`，形成分层目录。

## 4.5) 第二阶段（本轮新增）
- 新增 `dashboard/services/meridian_workflow_service.py`
  - 将经络图“通脉/开穴”执行编排（run 级别业务）从 `server.py` 迁出。
  - `server.py` 中 `meridian_tongmai_run / meridian_openxue_run` 改为薄封装，仅委派 service。
- `server.py` 删除了经络图运行期的大量私有业务函数（节点遍历、动作执行、日志拼装等）。
- 新增 `dashboard/services/meridian_ai_service.py`
  - 将“通脉决策 / 开穴详情”提示词拼装、JSON 解析回退、决策归一化逻辑迁出 `server.py`。
  - `server.py` 中 `meridian_tongmai_decision / meridian_openxue_detail` 改为薄封装，仅委派 service。

## 5) 第二阶段待完成（下一步）
- 抽离经络图业务核心（通脉/开穴）到 `services/meridian_service.py`。
- 抽离 PM/JZG/Automation 的 POST 分发到独立 `api/*.py` 模块。
- 为每个领域补充模块级单测（参数边界、错误码、状态流转）。
