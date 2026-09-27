# V1 第一阶段交付记录

日期：2026-09-28。范围：离线核心引擎；未连接现有 Controller，不涉及生产 Bot。

## 新增文件

- `.gitignore`：排除 runtime、测试数据、环境文件和构建缓存。
- `pyproject.toml`：Python 3.10+ 项目定义，零运行时第三方依赖。
- `src/tg_testcase/__init__.py`：公开 Engine、Store。
- `src/tg_testcase/models.py`：状态机、来源/评审/追溯领域模型。
- `src/tg_testcase/storage.py`：路径约束和原子写入。
- `src/tg_testcase/store.py`：SQLite 权威状态、版本历史、唯一活动任务、恢复。
- `src/tg_testcase/sources.py`：解析接口和 TXT/MD 支持。
- `src/tg_testcase/review.py`：评审、问题去重、关系校验。
- `src/tg_testcase/gates.py`：版本确认、P0/来源/草稿生成门禁。
- `src/tg_testcase/coverage.py`：业务路径证据和覆盖报告。
- `src/tg_testcase/excel.py`：原模板导出及重新读取校验。
- `src/tg_testcase/engine.py`：授权应用入口与完整工作流。
- `tests/test_core.py`：42 项自动化测试。
- `examples/offline_demo.py`：合成业务离线端到端示例。
- `README.md`：运行、边界、恢复、安全和未实现内容说明。
- `docs/PHASE1_DELIVERY.md`：本记录。

原有 `docs/V1_REQUIREMENTS.md`、`templates/testcase_template.xlsx` 未修改。运行示例另在被忽略的 `data/demo/` 创建合成 Task、数据库、版本快照、Excel 和覆盖报告；不是应提交源码的一部分。

## 验证结果

`PYTHONPATH=src python3 -m unittest discover -s tests -v`：42 项通过，包含状态迁移矩阵、并发唯一活动任务、CAS 并发更新、cancel/reset、正式/草稿门禁、确认失效、覆盖证据、AI 排除、崩溃恢复、快照修复、原子写失败、事务回滚、路径安全、模板保真和公式注入防护。

`PYTHONPATH=src python3 examples/offline_demo.py`：成功生成 1 条真实合成用例，覆盖 1 条显式业务路径，写出 Excel 和覆盖 JSON；示例结束取消 Task 并保留文件。

首次测试暴露 Python 3.10 不支持 StrEnum，已改用 str + Enum 并调整 Python 版本声明；最终无测试失败。未运行打包安装或桌面 Excel GUI 验证。

## Excel 回归

工作表名称、12 列、首行冻结、列宽、标题样式、数据行样式及其他模板 XML/ZIP 成员保留；第二行开始为真实数据；G～L 空白；无额外 sheet/追溯列。对 `= + - @`、空白前缀、XML 特殊字符做文本单元格回归。输出重新读取结构校验成功。

模板 SHA-256（开发前后相同）：

```text
f9b29ba0a28105cd5d8bb00fd749fc03665f65fbc2f050ca7504b7337d69f2a6
```

## 安全与限制

未读取真实环境文件或任何认证资料；未连接 Telegram、Notion 或其他外部服务；未安装或上传依赖；未执行 commit/push、权限提升或修改系统配置；未修改 TG Codex Agent。开发文件与测试/示例数据均位于本项目。

当前环境的 `.git` 不能作为可用 Git 仓库读取，`git status` 返回 `not a git repository`，因此无法提供 Git 索引级差异或已跟踪文件排除验证。文件清单基于本次创建的项目文件，runtime 排除由新增 `.gitignore` 声明。

两次指定历史分析原文未出现在项目中，无法逐条复核。未实现能力与下一阶段建议见 README；本次交付不代表完整 V1 或真实业务验收通过。
