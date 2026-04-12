# PM小组专家小组：文档管理与智能分析一期方案（2026-04-12）

## 目标

先交付一期可落地闭环：
- 文档上传/删除
- 目录分类
- 拖拽归档
- 文档详情展示

二期再接入：
- Agent 深度分析
- 问答联动检索

## 一期范围定义（按当前理解直接推进）

### 1. 数据模型

文档实体（建议）：
- `id`
- `projectId`
- `folderId`
- `group`（固定 `jzg`）
- `name`
- `ext`
- `size`
- `uploader`
- `uploadedAt`
- `analysisStatus`（一期默认 `pending`）
- `tags`（可选）

目录实体（建议）：
- `id`
- `projectId`
- `name`
- `order`

## 2. 页面能力

在 PM小组「专家小组」下新增三栏：
- 左：目录树（支持新增、重命名、删除）
- 中：文档列表（支持上传、删除、拖拽到目录）
- 右：文档详情（元数据、简要摘要占位、操作记录）

## 3. API 草案

- `POST /api/jzg/doc/upload`
- `POST /api/jzg/doc/delete`
- `POST /api/jzg/doc/move`
- `POST /api/jzg/doc/folder-create`
- `POST /api/jzg/doc/folder-update`
- `POST /api/jzg/doc/folder-delete`
- `GET  /api/jzg/doc/list?projectId=...&folderId=...`
- `GET  /api/jzg/doc/detail?projectId=...&docId=...`

## 4. 二期预留

### 分析产物结构（建议）
- `summary`
- `keywords[]`
- `knowledge_chunks[]`
- `risk_notes[]`
- `analysisBy`
- `analysisAt`

### 问答联动策略（建议）
- 默认按「项目 + 专家小组 + 当前目录」检索
- 可选全库检索开关
- 输出要附“命中文档引用”

## 5. 验收口径（一期）

- 可上传并在列表看到文件
- 可删除文件并同步刷新
- 可拖拽文件到其他目录
- 可查看文件详情信息
- 刷新页面后数据仍可恢复（持久化）

