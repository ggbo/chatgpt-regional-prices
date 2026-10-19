# 实施与核验记录

记录日期：2026-10-19（北京时间）。本文件区分本机扫描、GitHub runner 和 Pages 部署；不把一种成功替代另一种。

## 首次本机全目录真实扫描

运行环境：Windows，本工作区 Python 3.12。总耗时 803.375 秒。

| 项目 | 实际结果 |
| --- | ---: |
| 配置目标 | 252 |
| 国家与地区目录项 | 250（249 ISO + XK） |
| 辅助配置 | 2（EU、US2，不计国家排名） |
| 已尝试 / 未尝试 | 252 / 0 |
| 解析成功的配置 | 7 |
| 五档齐全的配置 | 7 |
| 有效套餐报价 | 35 |
| HTTP 403 配置目标 | 245 |
| 访问受限的套餐单元 | 1,225 |
| 完整矩阵单元 | 1,260 |

成功目标为 AI、HU、NC、SI、SX、TO、VI。US、PH、CL、JP、IN 在本机此次请求全部返回 403；它们是解析回归样例，测试夹具的金额没有填入生产快照。

全目录处理完成不等于全地区均取得价格。245 项缺口可在覆盖矩阵逐项检查，不能从排行榜消失，也不据此认定当地无套餐或不受支持。

官方原始 JSON 按响应字节 SHA256 保存，公开候选包含 35 条真实报价及全部 1,260 个状态。入口为 `public/data/manifest.json`，具体文件名随内容散列变化。

Frankfurter 实际请求于 `2026-10-19T11:57:07.480086Z` 成功，取得同一快照 USD/CNY/EUR/HUF 四种汇率，业务日期为 2026-10-19。35 条报价均可换算 CNY 和 USD。FX 快照为 `3892ac2b1c5e9d22584f147d20cb7ec8e7284399bad9dd3141167dfe7f96eb5e`，原始响应 SHA 为 `663b6a48c387d00c1b1ae56faa28e044872b12b49b4aaa60ee194fa91694c584`。

## 参考覆盖与独立支持状态

五档参考页共核对 697 条地区/渠道身份：Web 386、iOS 235、Google Play 推测 76，178 个地区 ID，零未映射。Go 页为 170 地区、235 条报价。参考价格金额未导入本站。

官网支持清单独立关联：支持 208、未列入支持清单 41、未知 3。该状态不控制是否请求价格。详见 `config/support-status.json`、`config/reference-coverage.json`、`docs/SOURCES.md`。

## 已运行验证

- TypeScript 类型检查通过，前端关键逻辑 3/3 通过。
- Python 回归 29/29 通过，覆盖金额精度、地区回退、缺失字段、独立金额合并、异常复核、失败保留、汇率、全矩阵和统计。
- 临时 bare Git 仓库验证：未完成进度可由新 runner 恢复，不替换上次有效快照；解析代码变更拒绝旧进度；完成周期清除检查点。
- Python 静态语法检查、`git diff --check` 通过。
- Vite 构建和真实本地 HTTP 项目路径检查通过：页面、查询分享链接、JSON、JS、CSS 均可加载；1,260 单元及证据散列校验通过。

## GitHub runner 与发布核验

[首次代码检查](https://github.com/ggbo/chatgpt-regional-prices/actions/runs/39248950502)在 Ubuntu / Python 3.13 / Node 24 上通过。

[首次采集运行](https://github.com/ggbo/chatgpt-regional-prices/actions/runs/39248950729)实际尝试 252 个配置，全部返回 HTTP 403，成功数 0。保护规则拒绝候选，未替换有效快照，也未部署空站。失败 artifact 已下载核对，永久摘要见 `reports/github-runner-initial.json`。

已将前述真实本机全量扫描、35 条报价及实际汇率快照初始化到独立 `data` 分支，并单独记录 runner 失败。它们保留原始核实时间。main 代码构建复用该有效版本，定时/手动采集仍执行正常刷新；没有放宽全源失败检查。网页显示采集受限横幅。

[第二次发布运行](https://github.com/ggbo/chatgpt-regional-prices/actions/runs/39249650543)已在 GitHub runner 完成数据恢复、类型及回归检查、构建、项目路径 HTTP 校验，并成功 fast-forward 持久化到 `data` 分支，提交为 `123eb8eaf78444c9900cc5f0ba2f6a43fc76b8ce`。

Pages 尚未上线。首次创建站点步骤被 GitHub 拒绝：`Create Pages site failed: Resource not accessible by integration`。当前连接器没有 Pages 设置写入工具，且环境无可用浏览器。需要管理员在仓库 Settings → Pages → Source 选择 GitHub Actions，然后重跑发布工作流。预期地址为 `https://ggbo.github.io/chatgpt-regional-prices/`；目前不能将该地址声称为已部署网站。

该阻塞发生在测试、构建和数据持久化之后。不存在以空数据或模拟价格规避检查的部署。完整机器收据为 `reports/deployment-attempt.json`。

## 未完成的核验与范围限制

- 大量配置在本机受到 403 访问限制，必须保留真实覆盖缺口。
- 当前环境没有可用浏览器，未执行实际渲染的视觉或点击检查；已完成类型、逻辑、产物及 HTTP 检查。
- iOS、Google Play 尚无价格适配器；已逐项标明渠道范围，不宣称复现三渠道 235 条有效报价。
- 账户可购买性未验证；只观察公开配置。
- 尚未观察下一轮定时执行，价格是否过期由实际成功时间计算。
