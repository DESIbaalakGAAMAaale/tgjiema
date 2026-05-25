# 项目规则

## Git 提交规则
- 每次完成代码修改后，**自动执行 git 提交和推送**到 GitHub，不需要用户提醒
- 提交前先 `git status` 查看变更，再用 `git diff --stat` 了解改了什么
- 提交信息用中文，简洁描述本次修改内容
- 如果用户要求推送，立即执行 `git add .; git commit -m "描述"; git push`（PowerShell 兼容语法）