# 运行与补查规则

网站只展示官网直购的五档常规月价，默认税状态为全部、人民币换算。价格表显示原价与约合人民币，不显示税状态列；金额税标仍保留在数据和详情中。顶部只保留新鲜报价数与最低地区价，不显示采集状态面板、覆盖统计、数据版本或生成时间。

税筛选会选择官方实际返回的金额类型：全部优先当前含税价，没有含税价再使用未税价；含税或未税仅使用相应税标的金额。官网主展示金额和有效的官方另列金额均可参与选择，详情保留各自来源及核实时间。切换税状态时原价、人民币估算、排序、最低价与 CSV 一起更新，不自行加税或去税。

完整目录已经执行首次全量扫描。之后分成两个阶段：

- 初始暂缺立即循环补查，成功地区立即退出，不限制尝试次数。次数只用于审计，写入数据分支，换 runner、恢复检查点或重跑不会清零。旧版本因十次上限停止的暂缺地区会恢复补查；持续受限的地区仍保留真实缺口。
- 正常阶段每小时第 23 分钟轮转最久未尝试的少量成功地区，目标窗口 72 小时。每小时第 53 分钟只补查失败项；正常轮转失败的地区也进入同一队列。只有五档都成功通过校验才退出。

所有采集保持单并发，请求起始至少相隔 2 秒。一个初始补查 job 最多运行约 25 分钟的采集，先持久化真实结果和次数，再显式触发下一轮；不以触发 data 分支 push 的方式间接发布。半小时补查没有到期目标时，不抓成功地区，也不刷新汇率。

`data:collection/ledger.json` 是长期账本，`data:collection/last-run.json` 是最近采集收据。有效价格位于 `data:state/`；两者独立。全批失败或检查失败仍保存账本，保留上次有效网站，不给旧价格更新核实时间。查看 [Actions](https://github.com/ggbo/chatgpt-regional-prices/actions) 和[最新账本](https://github.com/ggbo/chatgpt-regional-prices/blob/data/collection/ledger.json)。

界面代码更新由只读发布工作流立即使用最新有效快照构建，不等待采集队列，也不写入数据分支。定时及手动采集仍由原工作流串行持久化；两种工作流的 Pages 部署共用发布锁。持续初始补查会同时处理到期的普通失败，并优先保障接近 72 小时窗口的健康地区，避免暂缺队列长期占用预算。

GitHub 支持通过 `GITHUB_TOKEN` 显式触发 `workflow_dispatch`；继续补查 job 只申请本仓库 `actions: write`，不需要用户提供 ChatGPT Token 或个人访问令牌。[官方触发说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。

## 本地操作

Node 24、Python 3.13（兼容 3.12），Python 仅用标准库。使用现有 npm lockfile：

```sh
npm ci
npm run check
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q scripts tests
```

恢复数据分支后，可以选择普通轮转、只补失败或初始补全：

```sh
python scripts/pipeline.py prepare --mode refresh
python scripts/pipeline.py prepare --mode retry
python scripts/pipeline.py prepare --mode fill
npm run build
npm run verify:dist
```

维护者可在完成首次目录扫描后指定实际需要复核的地区，例如 `--mode retry --targets PH,CL`。`--resume` 只恢复同一轮真实观察，不把旧时间改成现在。价格或支付覆盖金额的大幅变化仍按原复核规则隔离。

公开仓库的标准 runner、默认 `github.io` 地址及静态站方案无需独立服务器。失败诊断 artifact 保留 3 天；长期必要证据按内容 SHA256 去重存储。需定期检查仓库大小和平台政策，不能承诺平台永久免费。

持续 403、404、缺少套餐字段或缺少汇率时，不能保证每个目录项最终都有可比价格。页面和矩阵保留真实缺口；目录完整、已经尝试与成功取得价格分别计数。

## 已有采集证据合并

Merge verified collection observations 与在线采集共用数据写入锁，取得最新 data 分支后才合并真实检查点。导入先核对文件散列和官方原始响应，再按原始核实时间重新解析；旧观察不会覆盖更新的结果。导入不请求官网报价，不能把已有证据改成刚刚采集。每次候选通过构建与证据检查后才普通推送，最后发布同一快照。

当前优先补齐菲律宾和日本的暂缺报价；智利、埃及和美国已有五档价格，不再为这次优先补查重复请求。已有价格继续保留，正常轮转与到期失败补查仍按既定时间运行。

菲律宾与日本本轮通过官方页面读取工具取得完整价格 JSON。此类证据明确标为 `official_page_reader` 与 `extracted_json_text`，HTTP 状态为未知，并保留完整工具输出及独立取证收据；不能称为 runner 成功收到 HTTP 200 或 HTTP 响应原始字节。实际金额仍逐档重新解析官方内容，不使用第三方价格或按比例推算。
