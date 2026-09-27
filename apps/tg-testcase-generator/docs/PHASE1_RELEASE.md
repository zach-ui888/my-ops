# Phase 1 release

本次为 V1 第一阶段首次 Git 发布准备（2026-09-28），交付离线核心引擎；不代表完整 V1 或生产业务验收完成。

## 测试结果

执行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v`，42/42 通过，无失败或错误。

## 模板哈希

`templates/testcase_template.xlsx` 未修改，SHA-256：

```text
f9b29ba0a28105cd5d8bb00fd749fc03665f65fbc2f050ca7504b7337d69f2a6
```

## 已实现范围

- 离线 Engine 入口、白名单/私聊/所有者校验、任务生命周期、取消与重置。
- SQLite 状态、版本历史、并发约束、原子文件写入与中断恢复。
- 来源保存、TXT/MD 解析、其他输入的明确 unsupported 状态。
- 结构化需求与评审、P0/P1、用户回答及最终规则追溯。
- 当前版本明确确认、正式/草稿生成门禁、模板保真 Excel 导出及校验。
- 基于来源链与显式验收证据的反向覆盖检查、AI 补充路径排除。

## 未实现范围

- Telegram Controller、生产 Bot 和 Notion 认证/读取。
- PDF/DOCX/XLSX 输入解析和图片 OCR。
- AI 自动需求提取、冲突发现、用例编写、自然语言回答及修订解析。
- 调度队列、自动重试、数据库跨版本迁移、备份调度与生产部署。
- Excel/LibreOffice GUI 验收及真实业务端到端验收。

## Git 发布边界

runtime/Secret 不进入 Git：排除 `data/`、`runtime/`、`.test-runtime/`、`output/`、SQLite/DB 及附属文件、Python 字节码、pytest/coverage 缓存、临时文件、日志、真实 `.env`、Token、认证会话和私钥。环境示例若需要，仅使用占位符。

本次只准备首次 Git 发布的正式交付文件，不执行 Git commit/push。
