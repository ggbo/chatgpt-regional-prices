# ChatGPT 各地区订阅价格

独立的 React / Vite / TypeScript 静态网站。Python 采集器读取公开官方地区配置，GitHub Actions 完成采集、校验、持久化和 Pages 发布。浏览器只读本站快照，无需登录或 ChatGPT Token。

范围为**官网网页、常规月付、主展示金额**：Go、Plus、Pro 100、Pro 200、Pro 500。逐行保留税标；人民币和美元为汇率估算，不代表最终扣款。

仓库：<https://github.com/ggbo/chatgpt-regional-prices>

预期项目地址：`https://ggbo.github.io/chatgpt-regional-prices/`。是否上线以 Actions 部署和线上 manifest 为准，有源码或成功写入数据分支不等于部署完成。

## 完整目录与真实缺口

初始目录有 249 个 ISO 国家与地区、单列 XK，以及不参加国家排名的 EU、US2 辅助配置。当前合计 252 个请求目标、1,260 个套餐单元；数量由版本化目录计算，不作为未来的固定上限。

每个目标都有五档结果；无报价地区仍可搜索。覆盖矩阵区分访问受限、请求失败、地区不匹配、字段缺失、金额异常、未尝试和历史报价。支持清单独立保存，不用于缩小采集目录。配置存在不表示账户实际可购买。

参考基准只保存地区、渠道、套餐、周期和来源身份，不导入第三方金额。逐项差异在 `reference-comparison.json`。一期只采集官网，iOS / Google Play 属于明确列出的待接入渠道，商店推测值不当作已验证单价。

## 本地运行与验证

环境为 Node.js 24、npm、Python 3.13。Python 代码兼容 3.12，仅使用标准库。

```sh
npm ci
npm run dev
```

无生产快照时页面显示加载失败，不填充演示价格。执行真实采集并验证：

```sh
python scripts/pipeline.py prepare --mode refresh --state-dir data-state --candidate-dir work/candidate --public-dir public/data
npm run check
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q scripts tests
npm run build
npm run verify:dist
```

失败的候选诊断仍保存在 `work/candidate`，非零退出不替换公开快照。单独运行全目录扫描：

```sh
python scripts/collect.py --output work/local-scan.json --checkpoint work/collector-checkpoint.json
```

checkpoint 用于同一次采集续作。新一轮使用新路径，不能把旧观察当作刚刚重新读取。默认并发 2，请求超时 15 秒，整轮预算 900 秒；403 不绕过、不引入用户凭据。预算内未完成的目标明确标为 `not_attempted`。

Actions 将未完成周期保存到 `data:progress/`，下轮仅在目录、套餐映射和采集代码散列一致且未超过 72 小时时续作，保留原观察时间，优先处理剩余目标。完整周期通过校验后清除进度。它与正式 `state/` 隔离，不充当有效价格快照。

## 金额、汇率与证据

官方来源路径为 `https://chatgpt.com/backend-api/checkout_pricing_config/configs/{CONFIG_ID}`，没有稳定 API 合同保证。映射集中在 `config/plans.json`；只读取五档 `month`，忽略年付、促销和 Business。

Decimal 解析并导出十进制字符串。`amount=8` 表示 8 个主币单位，不按 `minor_unit_exponent` 再除以 100。主展示与 `psp_override` 独立保留证据、状态、核实和变化时间；不互相覆盖、不按税率反推修正。

变化超过 20% 或币种、税标变化产生待复核项。维护者检查候选证据后在 `config/accepted-changes.json` 接受特定指纹或响应 SHA，记录复核原因，不能关闭所有检查。

汇率采用 [Frankfurter v2](https://frankfurter.dev/)，保留实际业务日期、提供方和原始响应 SHA。USD 明确为 1，其他币种缺失不按 1 换算。整批失败保留上一批并标旧，不拼接批次。

```text
USD 估算 = 原币金额 / R[原币]
CNY 估算 = 原币金额 / R[原币] × R[CNY]
```

换算使用 50 位十进制精度；前端按未舍入字符串比较，缺失值升降序都在末尾。列表、摘要和 CSV 共用选择规则。72 小时未核实的价格随浏览器时间成为历史数据，不参与当前最低价。

## Actions 与 Pages

主工作流 `.github/workflows/pages.yml`：

- 北京时间每天 03:23、15:23 定时刷新，也支持手动运行。
- main 更新通常复用有效快照；首次 runner 无数据分支时执行全量采集。
- 只有数据验证、测试、构建和产物校验通过后才提交白名单路径到 `data` 分支。
- 普通 fast-forward push；数据分支发生竞争写入时终止重跑，不强推。
- 上传并部署同一 `dist`。全源失败不发布空站，诊断 artifact 保留 30 天。

流程尝试通过 `configure-pages` 启用 Actions 来源。若 GitHub 权限策略拒绝，需要仓库管理员在 Settings → Pages → Source 选择 GitHub Actions 后重跑。长久状态在数据分支，不把 artifact 或 CI cache 当数据库。

定时运行可能延迟或停用，页面依据实际核实时间计算新鲜度。[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。采集和部署在同一工作流继续执行，不依赖 `GITHUB_TOKEN` 推送自动触发另一条工作流。

Vite 默认 `base=/chatgpt-regional-prices/`，自定义域名用 `VITE_BASE=/`。JSON 使用 `import.meta.env.BASE_URL`，分享状态用查询参数，不需要服务端路由回退。

## 文件职责

| 路径 | 内容 |
| --- | --- |
| `src/` | 搜索、筛选、精确排序、矩阵、详情、下载、同版本加载 |
| `scripts/pricing.py` | Decimal 归一化、独立金额类型和税标 |
| `scripts/collect.py` | 全目录调度、检查点、失败与变更复核 |
| `scripts/fx.py` | 同批汇率及精确换算 |
| `scripts/pipeline.py` | 候选验证、manifest、构建收据与持久化 |
| `scripts/reconcile_reference.py` | 参考身份覆盖差异 |
| `config/` | 地区、货币、五档、支持状态、参考基准和复核记录 |
| `schemas/` | 快照及覆盖矩阵 JSON Schema |
| `tests/` | 关键风险回归，测试数据不作为生产证据 |
| `docs/CONTRACT.md` | 前后端数据合同 |
| `data:state/` | 通过验证的快照、覆盖与运行状态 |
| `data:evidence/` | 按实际响应字节 SHA256 去重的公开 JSON |
| `data:history/` | 本币价格变化时的压缩变更记录 |
| `public/data/` | 构建绑定的不可变版本文件和 manifest |

客户端校验字节数、SHA 和同版目录/价格/汇率后整体替换视图；刷新失败保留已有视图。持久化收据绑定代码 HEAD、数据 HEAD、候选文件字节和构建 manifest。数据验证成功与部署成功独立记录，线上版本以已部署的 manifest 为准。

仓库增长需要定期评估，删除工作树旧文件不会消除 Git 历史体积。

## 实际交付状态

实际扫描、测试、覆盖缺口和部署结果见 `docs/IMPLEMENTATION-STATUS.md`（运行后生成）。未验证项据实列出，不保证每个地区返回价格或任何账户具备购买资格。
