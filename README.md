# 工程图 CU 提取与证据差异

Python CLI，使用 **Azure Content Understanding GA `2025-11-01`** 对两份原始 PDF
分别进行全页 OCR/layout 与领域结构化提取，再在本地保守配对。不是仅凭 LLM 看图总结。
支持 BOM、插头认证印字、线材印字、包装/标签、尺寸公差、备注及图框字段。
文件名/料号不用于推断新旧顺序，必须由调用者指定。

## 隐私与前提

- 只向明确授权的现有 Azure CU 资源上传输入，不使用第三方解析服务。
- **客户 PDF、原文、图片、报告、原始响应、凭据不得提交到 GitHub，包括私有仓库。**
  默认 `output\` 和 `local\` 被忽略。建议将实际结果写在仓库外的持久本地目录。
  `.gitignore` 不是安全边界，提交前必须检查暂存内容。
- Azure CLI 已 `az login`，当前身份有 CU 数据平面及自定义 analyzer 创建权限。
  使用内存中的 Entra token，不读取/打印 API key，不保存 token。
- 先确认资源地域、CU `processing_location`（如 `geography`）和模型部署 SKU 的数据边界。
  **资源在某地域不意味着 GlobalStandard 模型处理仅发生在该地域。**
- 只创建有 schema hash 名称的自定义 analyzer；不创建 Azure 资源/模型部署，
  不修改共享 defaults、容量或模型。需在使用前取得该操作的授权。
- `config.example.json` 中 deployment_versions 必须记录实际模型版本和 SKU；
  模型升级后更新它，避免误复用缓存。实际配置存 `local\config.json`，不含密钥。

## 运行（PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
New-Item -ItemType Directory -Force local
Copy-Item config.example.json local\config.json # 填写真实配置

.\.venv\Scripts\python -m cu_diff.cli diagnose --config local\config.json

.\.venv\Scripts\python -m cu_diff.cli extract `
  --config local\config.json `
  --old C:\approved-input\old.pdf --new C:\approved-input\new.pdf `
  --output C:\local-results\drawing-diff --allow-azure-upload

.\.venv\Scripts\python -m cu_diff.cli compare `
  --old C:\local-results\drawing-diff\old.response.json `
  --new C:\local-results\drawing-diff\new.response.json `
  --output C:\local-results\drawing-diff
```

`extract` 保持 OCR 开启，即使 PDF 有少量原生文本；工程图大量文字可能是矢量轮廓。
同一输入 SHA256 + 完整 analyzer + API + 模型映射/版本 + 处理边界缓存已完成结果。
异步操作先保存地址，超时重跑会续查该作业，不会自动重新发起计费请求。
失败作业保留其记录并明确报错；要重新计费尝试，先调查错误，再有意识地移走对应
`cache\*.operation.json`。不能把空结果当成“无变更”。

每次请求显式传 `modelDeployments`，不修改资源默认映射。响应中的真实 usage、
延迟和 HTTP 状态本地保存；不凭 token 估算金额。CU 提取、contextualization 和
Foundry 模型分别计费，最终金额需以 Azure 账单为准。

CU 在创建 analyzer 阶段会验证模型默认映射，早于请求级 override。
因此 analyzer 使用已有 `prebuilt-analyzer-completion` 别名，分析请求将该别名
显式映射到配置指定的模型部署；绝不依赖其可能指向其它模型的资源默认值。
实际所选模型及完整映射保留在 metadata 中，最终 usage 也应核对模型名称。

`diagnose` 只读检查 CU 实际 supportedModels 和默认映射，不上传文件。
存在 Azure OpenAI 部署并不等于 CU 支持该模型。`extract` 遇到不支持的模型会在
创建 analyzer/上传文件之前停止；不会偷偷换成其它模型。

## 输出与证据

| 文件 | 内容 |
|---|---|
| `old.response.json` / `new.response.json` | 完整 CU 字段、OCR、表格、图形、source/confidence、usage |
| `*.inspection.json` | SHA256、物理页尺寸、原生词/位图/矢量路径数量 |
| `*.metadata.json` | 输入 hash、analyzer/schema、模型版本、操作地址与实际耗时 |
| `api-events.json` | 当前运行的 HTTP 状态、请求 ID、延迟；可能含服务错误，按敏感结果保管 |
| `comparison.json` | 原文与保守标准化值、配对方法、提取/匹配不确定性、覆盖计数、OCR 辅助差异 |
| `comparison.zh.md` | 中文证据差异表；候选与未配对项必须复核 |

只有空白和 Unicode NFC 做比较归一化；保留原始字符，标准号/料号/尺寸不转数字。
唯一功能角色键优先配对，其余用保守的文字/坐标候选；不把变更后的料号作为身份键。
全量 OCR 是独立遗漏检查通道，不能将其与字段差异计数相加。
保留未配对区域、低置信度的“相同”内容和服务警告；高置信度不是正确率保证。
BOM 先以稳定部件描述而不是位置行号配对，避免行重排造成错配。生成的解释文字
不同但原文一致时，单列 `interpretation_only`，不计为原文修改。

### 小字复核

从原始矢量 PDF 重新渲染，不放大已有低分辨率截图。以 PDF pt（左上原点）指定裁剪：

```powershell
python -m cu_diff.cli crop --pdf C:\approved-input\old.pdf --page 1 `
  --rect 20 30 300 200 --dpi 600 --output C:\local-results\old-crop.pdf
```

生成裁剪 PDF 及 `.mapping.json`。将对应 old/new crop 用同样 `extract` 流程分析，
保留各自映射；CU inch 坐标乘 72 并加 crop 左上偏移才是原页 pt。全页结果必须先运行，
不能只分析预知变更区域。对图形/线条的变化，本实现保留描述与证据供人工检查，
**未实现经过标注验证的几何差异检测**，不声称自动识别全部图形变更。

## 验证与局限

```powershell
python -m unittest discover -s tests -v
```

测试只使用合成数据，不含客户文本。覆盖缓存与异步续跑、source、角色配对、重复/未配对、
OCR 拆并和不确定性。没有标注参考集时不能声称 70%、92%、95% 或完美召回。
认证印字只按文本比较，不判断认证有效性；材料不得从形状推断。
Hanwha 案例仅可借鉴“领域提取 + 证据关联 + 人工复核”理念，不能据此宣称复用了算法
或证明 CU 已在该案例生产使用。

官方参考（接口与可用模型应以当前资源响应为准）：
- [Analyzer 配置](https://learn.microsoft.com/azure/ai-services/content-understanding/concepts/analyzer-reference)
- [请求级模型映射](https://learn.microsoft.com/azure/ai-services/content-understanding/concepts/models-deployments)
- [GA Analyze REST](https://learn.microsoft.com/rest/api/contentunderstanding/content-analyzers/analyze?view=rest-contentunderstanding-2025-11-01)
- [文档提取](https://learn.microsoft.com/azure/ai-services/content-understanding/document/overview)

## 本地 Web 工程图审阅台（不部署）

```powershell
python -m pip install -e ".[web]"
python -m cu_diff.web --config local\config.json `
  --data-dir C:\local-results\web-sessions `
  --cache-dir C:\local-results\drawing-diff\cache `
  --port 8765 --allow-azure-upload
```

在浏览器打开 **http://127.0.0.1:8765**。服务只绑定 `127.0.0.1`，不创建云资源，
不执行部署，也不允许网页请求创建 analyzer。必须先有与当前 schema 匹配的现有分析器；
若缺失，按已授权的 CLI 初始化流程处理，Web 会明确失败而非修改 Azure 配置。
配置和 Entra 凭据仅在服务端。页面不访问外部 CDN、字体或分析服务。

**操作：** 分别上传“原图”“调整图”，即可查看本地渲染预览，不等待 CU。
仅支持内容有效、未加密的 PDF，单份最大 **20MB / 20物理页**。页码切换、缩放和
适应宽度只影响视图；原→新顺序始终由用户上传角色决定。点击“开始对比”后异步执行
现有 CU/缓存/配对流水线，展示排队、提取、配对、成功或失败状态。

文件标题显示上传原件的 SHA256 摘要（悬停查看完整值），覆盖面板保留完整原件及
CU分析文件hash。两侧字节相同会在前后端阻止对比，避免无意义CU调用；不同hash仅证明
文件字节不同，不证明每处画面不同。前端接收结果时核对两侧文件ID和hash，拒绝错源结果。

默认仅显示已配对的原文差异/行号重排候选，使用红色实框，通过编号联动两侧来源。
选择一项会定位到各侧证据所在物理页。
同一项可有多个源区域；只在有可靠 CU 原页坐标的一侧画框。没有来源、未知单位、
页尺寸不符、越界坐标会明确提示“无法定位”，不根据文字推测位置。
未配对项保留在“未配对待复核”范围，不混入默认差异索引；显式查看时使用黄色虚框，
详情提示“未配对到证据不代表原图没有”，不认定增删，也不为了成对显示而猜测另一侧框。
解释不同、一致项复核和OCR分段合并同样使用黄色虚框，不作为原文变更。选择“全部证据”
并勾选“显示仅解释差异”可查看生成解释；解释不同不计为原文差异。
低置信度、OCR辅助通道、未配对及覆盖限制需人工审阅，不能宣称完整几何变更检测。

### 旋转、缩放与缓存

- PNG 预览和 SVG 红框共享同一页面布局，位置按 CU 页尺寸归一化，CSS缩放/DPR不会
  更改证据坐标。页面图像最大2400像素宽/800万像素，用于审阅而非原矢量下载。
- 带90/180/270度旋转的上传文件保留原件，并生成 `remove_rotation()` 外观保持的
  本地分析副本。先按原始文件hash查找已有CU缓存，逐页核对缓存尺寸，并验证旋转规范化
  前后本地渲染像素hash一致（最多200万像素的校验图），才复用其坐标。不是仅凭宽高
  相同判断旋转/裁剪坐标正确。否则CU与预览使用同一零旋转副本，不猜测旋转变换。
  保存副本不生成随机PDF ID，重复上传可复用同一规范化缓存。普通零旋转PDF不改字节。
- `--cache-dir` 可指向已有 CLI `cache` 目录；命中缓存不会再次提交分析请求。
  `usage` 是原分析历史用量，不能当作缓存命中时新增计费。
- 不传 `--allow-azure-upload` 时是**只读缓存模式**：仍需 Entra 对已有 analyzer
  做只读核对；缓存未命中会明确失败，不自动产生计费上传。已提交异步操作可续查。
- 单个服务进程串行处理分析作业、最多排队4个，避免同进程重复写同一缓存。
  **不要让多个服务进程/CLI并发写同一缓存目录**；当前未实现跨进程锁。

### 本地隔离与生命周期

每个浏览器会话使用 HttpOnly/SameSite cookie、CSRF令牌和独立随机文件标识，不能访问
另一会话的文档/作业。上传文件名仅作显示，不参与磁盘路径。更换/删除任一文件立即失效
旧结果；若 Azure 请求已发出，无法撤销已发生的费用，旧作业结束后也不会重新展示旧框。
最多16个本地会话；闲置24小时的会话在后续请求/启动时清理，不是独立后台清理服务。

`--data-dir` 下的 `web-session-<随机值>` 保存上传原件/旋转副本和作业审计，不记录
客户文字到控制台。更换文件后不再被当前作业使用的旧PDF会清除。**CU缓存单独长期保留**，
不会随浏览器关闭删除。清理时先停止服务，再在资源管理器中删除指定会话目录；
若要清理计费缓存，针对所选文档的 `<cache-key>.response.json`、
`<cache-key>.metadata.json` 及操作记录逐一删除。删除缓存意味着下次可能重新计费。
不要删除或覆盖原始客户文件。所有运行数据应放在仓库外，或已忽略的 `local\` / `output\`。

当前是单机审阅工具，服务随启动它的进程/会话退出而停止；终端内可用 Ctrl+C 停止。
保留了标准 WSGI `create_app` 工厂，便于以后适配 Azure App Service；**本轮没有部署**。
公网部署前仍需组织认证、跨进程作业队列、存储策略和额外运行隔离，不应直接改成
`0.0.0.0` 暴露此本地模式。

### Web 验证

```powershell
python -m pip install -e ".[web,test]"
python -m playwright install chromium
python -m unittest discover -s tests -v
$env:CU_BROWSER_TESTS = "1"
python -m unittest discover -s tests -p test_web_browser.py -v
```

后端与浏览器测试只使用动态生成的合成PDF，不调用Azure。覆盖即时预览、多页联动、
两侧证据框在DPR=2和缩放/适应宽度下的坐标一致性、换文件清空、无源/单侧不造框、
失败反馈、会话隔离、CSRF/Host校验、上传内容/大小/页数限制和旋转外观保持。
真实客户文件的浏览器验证截图/记录只能写在忽略目录或会话持久目录，不能提交仓库。
