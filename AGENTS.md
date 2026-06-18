# AGENTS.md

## 代码修改注意事项
- 禁止直接在 main 分支上进行开发，需要根据需求，创建 branch，来承接修改任务
- **开始代码修改任务后，第一时间创建 feature branch 并切换过去**，不要在 main 上积累未提交的修改
- 在保证commit完整性的同时，尽可能多的提交
- 每个commit，都要保证能够编译通过，并且通过所有的测试

## 需求开发入口

当用户引用 GitHub Issue（URL 或 `#编号`）并要求修复/实现时，**必须**通过 `github-issue-to-pr` 技能完成：
```
run_skill({ name: "github-issue-to-pr", arguments: "<Issue URL 或 #编号>" })
```
不要跳过技能直接手动修复，除非用户明确要求快速修复且无需完整流程。

## 运行环境

### Shell 环境
- **类型**：Git Bash (MinGW64)，非 WSL、非 Windows CMD
- **路径格式**：使用 `/d/.github` 而非 `D:\.github` 或 `D:\`
- **可用命令**：`git`、`gh`、`ls`、`cat`、`grep`、`find`、`head`、`tail`
- **不可用**：`apt`、`apt-get`、`dpkg`（非 Linux）

### Python 环境
- **当前状态**：Python **不可用**（仅有 Windows Store 空壳，无法运行）
- **测试策略**：当无法运行 `pytest` 时，依靠 review 子 agent 验证代码正确性
- **测试命令参考**：`pytest tests/test_org_membership_cleanup.py -v`
- 本项目为 **Python** 项目，**不要**尝试 `tsc --noEmit`、`npm test`、`npm run lint`

### 校验命令
- 主脚本为 Python，用 review 子 agent 替代本地测试
- `gh` CLI 已认证（用户：nevstop），可直接使用 `gh pr create`、`gh issue comment` 等
- Git 操作正常，push/pull/fetch 均可用

### 常见错误
| 错误 | 原因 | 避免方式 |
|------|------|---------|
| `python` 返回 exit code 49 | Windows Store 空壳 | 不要调用 python，用 review 替代 |
| `pip` command not found | 未安装 | 不要尝试 pip install |
| `cd D:\` 失败 | Git Bash 用 `/d/` 格式 | 用 `cd /d/.github` |
| `tsc --noEmit` 不存在 | 这是 Python 项目 | 不要用 Node.js 命令 |
| `npm test` 不存在 | 同上 | 用 pytest 命令参考，或 review 替代 |
