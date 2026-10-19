# ChatGPT 全球订阅价格

独立的 ChatGPT 地区价格查询网站，使用 GitHub Pages 托管。页面展示 Go、Plus、Pro 100、Pro 200、Pro 500 五个常规月付档位，提供地区搜索、币种与状态筛选、人民币／美元估算、双向排序、原始价格详情和 CSV 导出。

前端只读取随站点发布的静态 JSON。Python 采集器在 GitHub Actions 中定时访问公开官方配置，校验后更新数据分支并发布同一份已构建产物。项目不需要常驻服务器、数据库、ChatGPT 登录凭据或汇率 API key。

## 地区覆盖与“235 条”的口径

`config/regions.json` 是完整采集目录：249 个 ISO 国家或地区条目，另列 `XK`，以及明确标记为地区组或内部配置的 `EU`、`US2`，合计 252 个配置目标。所有目标都建立五个套餐的状态行，因此完整矩阵为 1,260 行。`EU`、`US2` 不计作两个新的国家。

参考页的 Go 数据在 2026-10-19 核实时显示 170 个地区、235 条报价，包含 Web、iOS、Google Play 多个渠道。同一个地区可能有多条渠道记录。本项目当前采集 **Web 官方配置**，逐一查询完整地区目录，不通过复制渠道行或填补猜测价格来凑记录数。[参考页面](https://priceai.cc/official-prices/chatgpt__go-monthly)

页面分别展示目录目标数、已尝试数、未尝试数、有有效报价的地区数、五档齐全的地区数和报价数。失败地区仍可搜索；`request_failed`、`access_blocked`、`country_mismatch`、`plan_field_missing`、`not_attempted` 等状态都有明确区分。目录存在、配置返回价格、账户能够购买，是三个不同事实。

`config/reference-coverage.json` 保存参考站 Web 配置身份的有日期基线。每次准备数据时生成 `reference-comparison.json`，比较具体配置和套餐，列出官方新增发现与参考缺口，不导入参考站金额。

## 官方来源与价格解释

目前公开可读取的配置地址为：

```text
https://chatgpt.com/backend-api/checkout_pricing_config/configs/{CONFIG_ID}
```

例如 [美国](https://chatgpt.com/backend-api/checkout_pricing_config/configs/US)、[菲律宾](https://chatgpt.com/backend-api/checkout_pricing_config/configs/PH)、[日本](https://chatgpt.com/backend-api/checkout_pricing_config/configs/JP)。它是已核验的公开官方配置路径；本项目没有将它视作有稳定性保证的正式公开 API。上游路径、结构或访问策略变化时，需要维护采集器。

| 页面档位 | 官方配置键 | 读取路径 |
| --- | --- | --- |
| Go | `go` | `currency_config.go.month` |
| Plus | `plus` | `currency_config.plus.month` |
| Pro 100 | `prolite` | `currency_config.prolite.month` |
| Pro 200 | `pro` | `currency_config.pro.month` |
| Pro 500 | `promax` | `currency_config.promax.month` |

金额是货币主单位，原样保存为十进制字符串。例如 `8.0 USD` 是 8 美元；`minor_unit_exponent=2` 不表示还要除以 100。主报价使用 `month.amount` 与 `month.tax`；`psp_override` 单独展示并独立保存来源和核实时间，不反向计算或覆盖主报价。币种的精度字段可能位于顶层或 `currency_config` 内，两处均处理。

每份用于报价的 JSON 响应按实际 HTTP 响应体字节计算 SHA-256，保存在 `evidence/<sha256>.json`。当前快照、详情来源、文件清单和原始证据可以互相核对。测试目录内标记为研究工具摘录的 fixtures 仅用于解析回归测试，不充当线上 HTTP 采集证据。

官方套餐说明：[Go](https://help.openai.com/en/articles/11989085-what-is-chatgpt-go)、[Plus](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus)、[Pro 档位](https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers)。地区支持状态可独立参照 [OpenAI 支持国家与地区](https://help.openai.com/en/articles/7947663-chatgpt-supported-countries)，不以该列表预先跳过地区配置查询。

## 汇率与排序

汇率来自 [Frankfurter v2](https://frankfurter.dev/) 的 USD 基准混合汇率集。一次请求取得完整汇率响应，再选择报价需要的币种，避免某个未知币种令整批请求失败。原始响应、混合报价日期、各提供方对象及其实际日期都保存；跨日期情况明确标记。[汇率请求](https://api.frankfurter.dev/v2/rates?base=USD&expand=providers)

设 `R[币种]` 为 1 USD 对应的当地货币数量：

```text
美元估算 = 原币金额 / R[原币]
人民币估算 = 原币金额 / R[原币] × R[CNY]
```

计算使用 Python `Decimal`，除法保留 50 位有效数字，显示时才舍入。人民币为 CNY，不隐式替换成 CNH。缺失汇率保留原币价，换算值为 `null`；排序升序、降序均放末尾。获取汇率失败时整体保留上一份汇率集并标记过期，不将不同采集批次的单个币种拼成“新”汇率集。

原币价格在选择单一币种后可以按金额排名；未选择币种时按 ISO 币种分组后排序，不能把 100 日元直接当作比 20 美元更贵。税费状态随主报价一起显示。换算是展示估算，不是支付机构实际扣款承诺。

## 本地运行

环境：Node.js 24、npm、Python 3.12 或更高版本；工作流固定 Python 3.13。Python 采集部分只使用标准库。

```bash
npm ci
npm run dev
```

Vite 的开发服务器会在终端输出本地地址。前端加载 `public/data/` 中随项目提供的可审计快照，其实际采集时间和结果以页面与 `latest.json` 为准。

验证并预览生产构建：

```bash
npm run check
npm run test
python -m unittest discover -s tests -p 'test_*.py'
npm run build
python scripts/pipeline.py verify-dist --dist-dir dist
npm run preview
```

构建使用相对 `base: './'`，JSON 路径通过 `import.meta.env.BASE_URL` 生成。单页入口不使用需要服务器回退规则的路径路由，适配 `https://用户名.github.io/仓库名/`。产物检查覆盖本地资源的相对路径、完整地区矩阵、JSON 文件哈希及原始证据哈希；交互逻辑由前端测试和浏览器检查验证。

## 手动更新数据

在项目根目录执行：

```bash
python scripts/pipeline.py prepare --mode refresh --state-dir data-state --candidate-dir work/candidate --public-dir public/data
npm run check
npm run test
python -m unittest discover -s tests -p 'test_*.py'
npm run build
python scripts/pipeline.py verify-dist --dist-dir dist --candidate-dir work/candidate
```

`prepare refresh` 先采集并合并旧价格，再更新汇率、比较参考覆盖、生成文件清单并写入前端目录。默认并发 2、总采集预算 900 秒；可使用 `--concurrency` 和 `--budget-seconds` 调整。单独检查官方价格采集时可以执行：

```bash
python scripts/collect.py --regions config/regions.json --plans config/plans.json --state-dir data-state --out-dir work/candidate --budget-seconds 900 --concurrency 2
```

`prepare reuse` 在地区目录与套餐映射未变化时只重新打包数据分支已有快照；尚未建立数据分支时使用项目自带的初始快照，不访问价格或汇率源。若当前配置目录、目录版本或套餐映射与旧快照不同，会自动改为 `refresh`，先验证更新后的完整目录再发布：

```bash
python scripts/pipeline.py prepare --mode reuse --state-dir data-state --candidate-dir work/candidate --public-dir public/data
```

采集器退出码 `0` 表示可用候选版本，`2` 表示本轮没有新的有效价格，`3` 表示首次采集未完成全目录；后两种仍保存候选诊断，但流水线不覆盖或发布原来的站点。已有完整基线之后，部分地区失败可以保留旧记录、旧来源和旧核实时间，并发布其他地区的新观察。页面会显示本次失败和历史价格，而不会重新标记为刚刚核实。

## 发布到 GitHub Pages

### 1. 建立独立仓库

建议仓库名 `chatgpt-regional-prices`，默认分支 `main`。在 GitHub 创建空仓库，再把本目录推送进去；不要将其放入已有充值或兑换系统。将下面的 `OWNER` 替换为实际账号或组织：

```bash
git init -b main
git add .
git commit -m "Build official regional ChatGPT price site"
git remote add origin https://github.com/OWNER/chatgpt-regional-prices.git
git push -u origin main
```

已经初始化或已经配置远程的仓库跳过对应初始化命令。使用 GitHub 网页上传或已授权的 GitHub 连接提交同一套源文件也可以；应保留 `.github/workflows/pages.yml`、`package-lock.json` 和完整的 `public/data` 初始快照及证据。

### 2. 启用 Pages 与工作流权限

在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。[GitHub 官方步骤](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

工作流声明准备任务 `contents: write`，用于写入同仓库的 `data` 分支，以及 `pages: read`，用于读取已启用的 Pages 配置；部署任务声明 `pages: write` 和 `id-token: write`。若组织策略禁止这些权限，应由仓库管理员启用对应能力。站点不需要添加 ChatGPT Cookie、账号密钥、个人访问令牌或汇率密钥。

### 3. 完成第一次部署

打开 **Actions → Refresh prices and deploy Pages**。如果初次推送发生在启用 Pages 之前而使部署失败，启用后重跑该次工作流，或点击 **Run workflow** 触发新采集。

首次工作流会读取 `data` 分支；该分支不存在时，仅建立本地空分支。通过数据校验、前端检查、构建和产物校验后，才提交第一个有效快照并正常推送 `data`，无需手动预建该分支。源代码推送采用 `reuse`，定时和手动运行采用 `refresh`。

准备任务成功后，部署任务发布刚刚校验的同一份 `dist` 产物。Actions 的部署结果会提供站点 URL，通常为：

```text
https://OWNER.github.io/chatgpt-regional-prices/
```

连接 GitHub 账号与“可以创建仓库、设置 Pages”是不同权限。如果协作工具没有后两项能力，在 GitHub 网页完成这两个设置即可继续使用已写好的工作流，不需要改换实现工具或托管平台。

### 4. 定时采集与后续发布

工作流采用北京时间每天 **03:23、15:23**。GitHub 当前支持 IANA 时区参数；定时任务只运行于默认分支，可能排队延迟或被丢弃，公开仓库长时间无活动也可能停用。因此页面按实际核实时间计算新鲜度，不能把计划时间当成成功更新时间。[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

采集和发布在同一个工作流连续完成。`GITHUB_TOKEN` 产生的推送通常不会再次触发另一个 `push` 工作流，所以这里不依赖数据提交自动引发第二次部署。[GitHub 触发规则](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

## 持久状态和恢复

| 位置 | 内容 |
| --- | --- |
| `main` | 前端、采集器、配置、测试、工作流、可复现的初始快照 |
| `data:state/` | 最近通过构建校验的价格、覆盖、状态、参考比较、清单 |
| `data:evidence/` | 按响应体 SHA-256 去重的价格和汇率原始 JSON |
| `data:history/YYYY-MM/*.json.gz` | 每个已验证快照的完整压缩历史包 |
| Pages 部署产物 | 此次成功发布的前端与同版本静态数据 |

`checkout-state` 用临时认证程序读取 `GITHUB_TOKEN`，令牌不会写入 Git 远程地址、Git 配置、公开数据或网站。`persist` 只暂存允许的数据路径；核对准备阶段的本地 HEAD、当前远端 HEAD 和构建收据后，使用普通 fast-forward push。发现竞争写入时终止重跑，不强推覆盖。

数据分支成功更新而 Pages 随后部署失败时，线上仍是上一部署版本。查看页面的 `snapshot_id`、`data/manifest.json` 和 Actions 部署结果确认当前版本；不要仅凭数据分支最后一次提交判断上线成功。重新运行部署可以恢复。历史包可以解压用于审计和离线恢复；指定历史版本重新上线时，应创建明确的数据恢复提交，重新校验数据与构建，并保留现有 Git 历史。

## 文件导航

| 文件 | 职责 |
| --- | --- |
| `config/regions.json` | 全目录、配置身份与地区类型 |
| `config/plans.json` | 五档套餐与官方配置键 |
| `scripts/pricing.py` | 精确金额解析、字段验证、每个金额类型的历史合并 |
| `scripts/collect.py` | 有预算的全目录采集、失败分类、完整状态矩阵 |
| `scripts/fx.py` | Frankfurter 原始证据、精确换算、汇率缺口与过期回退 |
| `scripts/reconcile_reference.py` | 参考站 Web 配置身份覆盖比较 |
| `scripts/pipeline.py` | 数据准备、文件清单、构建验证、数据分支持久化 |
| `src/lib/prices.ts` | 筛选、精确排序、格式化与导出逻辑 |
| `.github/workflows/pages.yml` | 定时／手动采集与 Pages 发布 |
| `docs/CONTRACT.md` | 前后端及数据模式约定 |

这是地区官方配置观察工具。当前不采集需要登录的结账过程，也未集成 iOS 与 Google Play 的实际 SKU 价格。可继续按独立渠道扩展，前提是每条记录都有可核验来源、明确月付身份、原币金额和税费口径；不能将应用商店的价格范围端点当成已核验套餐报价。
