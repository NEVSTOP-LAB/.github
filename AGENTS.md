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

该技能涵盖完整流程：读取 Issue + 评论 → 分析代码 → 计划确认 → 测试先行 → 实现 → Review 循环 → 提交 PR。
不要跳过技能直接手动修复，除非用户明确要求快速修复且无需完整流程。
