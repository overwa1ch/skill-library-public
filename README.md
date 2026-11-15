# Skill Library

本地 Skill 注册表与只读 MCP 加载器。将技能正文保存在本地，通过 MCP 工具按需发现和加载，并保留脚本、模板与参考资料的原始目录。

本仓发布独立工具源码、合成测试夹具和使用说明。技能内容由使用者自行注册，按各自许可证使用。

## 功能

- 稳定技能 ID、来源标识、分类标签、启停状态和常驻选择。
- 读取原始 `SKILL.md`，返回正文摘要、技能目录和附件的相对路径基准。
- 每个允许自动触发的技能提供一个加载工具；只允许点名的技能通过 `load_explicit_skill` 加载。
- 尊重 `agents/openai.yaml` 中的 `policy.allow_implicit_invocation`，以及 SKILL.md frontmatter 中 Claude Code 的 `disable-model-invocation: true`。
- 支持 Codex、Claude Code、Claude Desktop、Cursor 等 MCP 客户端：`configure.py` 按配置文件扩展名写 Codex 的 `config.toml` 或其他客户端的 `mcpServers` JSON；MCP 提示语按客户端自报名称自动选择。
- 没有逐项隐藏开关的宿主，可用 `relocate` 把技能文件夹迁入 Library 自有目录，原件留档、可回滚。
- 检查来源可用性，停用来源和已确认失效的插件来源拒绝加载。
- 配置修改先生成计划，应用时备份，回滚保留无关的并发改动。

加载工具只返回技能说明。具体工作由模型使用现有文件、终端或插件工具执行。

## 环境与安装

Python 3.9+，当前以 macOS 验证。文件锁依赖 POSIX `fcntl`，暂不支持 Windows。

```sh
git clone https://github.com/overwa1ch/skill-library-public.git
cd skill-library-public
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python library.py --registry runtime/registry.json init
```

## 注册与管理

将下面的路径替换成真实技能文件的绝对路径。`source` 是唯一且稳定的来源标识。

```sh
.venv/bin/python library.py --registry runtime/registry.json register /absolute/skill/SKILL.md --source personal:example --scope user --tag writing
.venv/bin/python library.py --registry runtime/registry.json list
.venv/bin/python library.py --registry runtime/registry.json status
```

将返回的技能 ID 代入以下命令：

```sh
.venv/bin/python library.py --registry runtime/registry.json load ID
.venv/bin/python library.py --registry runtime/registry.json edit ID --enabled false
.venv/bin/python library.py --registry runtime/registry.json edit ID --enabled true
.venv/bin/python library.py --registry runtime/registry.json edit ID --tags research writing
.venv/bin/python library.py --registry runtime/registry.json edit ID --path /absolute/new-version/skill/SKILL.md
```

整个文件夹的技能可以一次登记：`scan /absolute/skills-folder --source-prefix personal:mine` 登记下一层每个含 `SKILL.md` 的子目录，来源标识为 `前缀/子目录名`。

注册、扫描和重绑路径不会复制或搬走技能文件，只有 `relocate` 会迁移（见下文）。`scope` 记录来源类型；初次注册时 `system` 默认常驻，其余默认由 Library 提供。后续可用 `edit ID --placement global` 或 `--placement library` 调整。注册表的修改与宿主展示配置分别管理。

### 多客户端共用一个库

各客户端的 MCP 配置使用同一份 `library.py` 与同一个注册表绝对路径。技能可以集中保存于 `runtime/sources/`；复制插件技能时保留完整包层级及其共享资源。旧注册表路径可作为指向中央注册表的符号链接；加载器解析真实路径后使用同一把锁和同一个原子写入目标。

`runtime/` 中的技能、注册表和客户端备份只保存在本机，不进入本仓源码发布。

## 接入 Codex

先注册技能，再生成配置计划。将配置路径替换成实际 Codex 配置文件的绝对路径。

```sh
.venv/bin/python configure.py plan --config /absolute/codex-home/config.toml --registry runtime/registry.json --output runtime/plan.json
```

检查计划中的路径和变更。可重复传入 `--id ID`，先选择少量技能。应用计划及回滚命令：

```sh
.venv/bin/python configure.py apply --plan runtime/plan.json --transactions runtime/transactions
.venv/bin/python configure.py rollback runtime/transactions/TRANSACTION_ID
```

计划管理 `mcp_servers.skill_library` 和所选技能的原生展示配置。插件连接单独管理。配置保留虚拟环境 Python 的绝对路径，不解析该路径的符号链接。

也可以手动添加 MCP 服务：

```toml
[mcp_servers.skill_library]
command = "/absolute/skill-library-public/.venv/bin/python"
args = ["/absolute/skill-library-public/library.py", "--registry", "/absolute/skill-library-public/runtime/registry.json", "serve"]
enabled = true
```

手动配置 MCP 只接入服务；原生技能目录的展示项需另行管理，避免重复发现。配置写入后，在宿主中核对工具列表和实际技能加载。

## 接入使用 JSON 配置的客户端

Claude Code、Claude Desktop、Cursor 等客户端需要支持 stdio MCP，并用顶层 `mcpServers` 对象登记服务。先查明客户端实际读取的配置文件，将下方路径替换为该文件的绝对路径。不写 `--format` 时，配置文件为 `.toml` 则使用 Codex 格式，其余按 `mcpServers` JSON 处理。

```sh
.venv/bin/python configure.py plan --format mcp-json --config /absolute/client-config.json --registry runtime/registry.json --output runtime/client-plan.json
.venv/bin/python configure.py apply --plan runtime/client-plan.json --transactions runtime/transactions
.venv/bin/python configure.py rollback runtime/transactions/TRANSACTION_ID
```

应用前检查计划。JSON 计划只管理 `mcpServers.skill-library`；应用时保留文件内其他字段的最新值，同名服务与计划不一致时拒绝覆盖。`placement: global` 的技能继续由宿主管理，JSON 计划只校验 Library 实际提供的技能。

也可以在客户端配置中手动加入以下服务，将每个路径替换为真实绝对路径：

```json
{
  "mcpServers": {
    "skill-library": {
      "command": "/absolute/skill-library-public/.venv/bin/python",
      "args": ["/absolute/skill-library-public/library.py", "--registry", "/absolute/skill-library-public/runtime/registry.json", "serve", "--host", "generic"]
    }
  }
}
```

`--host generic` 提供通用 MCP 提示语。技能文件的 `disable-model-invocation: true` 与 `agents/openai.yaml` 中的点名策略均会生效。应用或重新连接后，在客户端确认工具目录并加载一个已注册技能；合成测试不能证明客户端已经采用新配置。

Claude Code 也可以直接执行 `claude mcp add --scope user skill-library -- /absolute/.venv/bin/python /absolute/library.py --registry /absolute/runtime/registry.json serve`。`serve` 默认按客户端自报的名称选择提示语，只有名称为 `codex-mcp-client` 的客户端得到 Codex 措辞；可用 `--host codex` 或 `--host generic` 固定。

对于扫描技能目录但没有逐项隐藏开关的宿主，要让某个技能只由 Library 提供，可以把它的文件夹迁出宿主目录：

```sh
.venv/bin/python library.py --registry runtime/registry.json relocate ID --self-contained
.venv/bin/python library.py --registry runtime/registry.json relocate-rollback runtime/relocations/RELOCATION_ID
```

`relocate` 必须声明资源边界：独立技能用 `--self-contained`，确认脚本和附件均位于自己的目录内；插件及含共享资源的技能用 `--package-root /absolute/complete-package`。例如：

```sh
.venv/bin/python library.py --registry runtime/registry.json relocate ID --package-root /absolute/complete-package
```

整包模式保留完整目录布局，并在同一注册表事务内重绑包内全部登记项，包括停用项；含 `global` 技能的包拒绝迁移。默认目标为注册表旁 `sources/personal/` 或 `sources/plugins/`，可用 `--into` 指定父目录。声明的包根负责覆盖普通父路径和动态脚本依赖，程序不猜测包外资源。

目录字节、权限、空目录和符号链接逐项核对，原件保存在 `relocations/<记录>/original/`。版本化日志覆盖复制、发布、重绑和归档，每一步中断后均可使用输出的记录目录执行 `relocate-rollback`；重复回滚是安全的。副本保留在同一记录的 `library-copy/`，不删除数据。迁移期间新增/重绑包内登记项、原位置复用、副本被改动时拒绝覆盖。兼容原版本完整、可验证的迁移记录；无法证明完整性的旧记录继续保留并明确报错。

迁出会移除宿主原生 `/技能名` 入口，之后通过 MCP 点名加载。Git checkout 内的目录、跨文件系统归档、特殊文件、hardlink 和会因迁移失效的符号链接均拒绝；已经集中保存的来源无需再次迁移。

## 可选流程检查规则

`bootstrap.py` 可在全局 `AGENTS.md`、`CLAUDE.md` 或 `GEMINI.md` 添加一个独立管理的规则块，要求任务开始前发现并加载适用流程。只有 Codex 自己的 `AGENTS.md` 默认使用提到 Codex 工具名的措辞。先检查生成的规则和计划：

```sh
.venv/bin/python bootstrap.py plan --host codex --path /absolute/codex-home/AGENTS.md --output runtime/bootstrap-plan.json
.venv/bin/python bootstrap.py apply --plan runtime/bootstrap-plan.json --transactions runtime/bootstrap-transactions
.venv/bin/python bootstrap.py rollback runtime/bootstrap-transactions/TRANSACTION_ID
```

其他宿主可用 `--host generic` 生成通用规则，目标文件名支持 `AGENTS.md`、`CLAUDE.md` 和 `GEMINI.md`。以下示例仍须使用宿主实际读取的路径：

```sh
.venv/bin/python bootstrap.py plan --host generic --path /absolute/instructions/CLAUDE.md --output runtime/client-bootstrap-plan.json
.venv/bin/python bootstrap.py apply --plan runtime/client-bootstrap-plan.json --transactions runtime/bootstrap-transactions
```

该规则包含短任务的发现检查；是否安装应结合使用场景决定。只允许用户点名的技能仍要求明确点名。

## 测试与适用范围

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用合成技能和临时配置。工具发现方式与初始上下文开销取决于宿主；本工具不保证固定的 token 降幅，也不能移除同一对话中已经加载的正文。`tools/app_server_probe.py` 读取独立 app-server 的库存，其结果不代表正在运行的 Desktop 已采用配置。

## 许可证

[MIT](LICENSE)。
