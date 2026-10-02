# GS1 扫码核验 API

基于 FastAPI 的 GS1 条码解析服务,提供九个接口。交接、装拆与地点追溯的重放逻辑收拢在同一个**可逐事件推进的内核** `app/replay.py` 中,各接口在内核之上做各自的校验、适配与结果整理:

- **重放内核**(`app/replay.py`):以 GTIN + AI 21 序列号为共享身份,统一维护父子边、持有人、成功事件下标与地点历史;提供逐事件命令 `transfer` / `pack` / `unpack` 与三套策略配置(`ReplayConfig.packing()` / `.custody()` / `.trace()`)。每条命令先全部校验通过再一次性提交,失败命令不留任何半次更新。
- **召回核验**:上传一批扫描串和一批召回规则,逐条返回解析出的字段(GTIN / 批号 / 序列号 / 日期)以及命中的规则 ID。
- **签名召回核验**:扫描串之外,召回规则装在 compact JWS(EdDSA / Ed25519 签名)载荷里;先验签名、有效期与授权 GTIN 前缀,全部通过才返回 kid 与原扫码核验结果。公钥只来自本地信任文件。另支持**单包撤销**:配置本地 JSON 撤销名单后,名单命中的规则包(按 `SHA-256(header.payload)` 标识,与签名段编码无关)即使仍在有效期内也返回 403,不连带同一 kid 的其他包。
- **召回规则关系分析**:上传 2~100 条召回规则,上线前找出不可能命中任何合法扫描串的规则,以及命中集合有交集的规则对(等价 / 包含 / 部分相交),每对附一条同时命中两者的证据扫描串。
- **交接审计**:上传一批实物交接事件,按时间重放并审计每件实物(GTIN + 序列号)的持有人流转,返回成功事件与失败原因。
- **交接日志最少剔除**:上传 1~300 条现有格式交接事件及可选的锁定事件原下标,选择剔除总数最少的保留子序列(合法交接链、批号 / 有效期一致、锁定必留),返回处理顺序的保留下标、原下标升序的剔除下标与各保留实物的最终持有人。
- **装拆包装审计**:上传一批按顺序执行的装入 / 取出操作,重放并审计实物(GTIN + 序列号)间的直接包装关系,返回涉及实物的直接父件 / 子件 / 最外层容器与失败原因。
- **召回隔离计划**:上传库存扫描、召回规则、装拆操作与已封签实物扫描串,命中召回的实物沿最终包装父链归并到最高层已封签祖先,输出隔离单元;装拆重放继续使用同一内核包装入口 `packing.replay_packing`。
- **召回实物地点追溯**:上传库存扫描、召回规则与带时区的交接 / 装拆事件,按 UTC 升序重放(容器交接带动全部后代、取出保留当时持有人),仅对命中召回的库存实物返回其按首次到达顺序去重的保管地点。可选 `contact_until`:提供时额外返回**未命中召回**的库存实物与**直接命中**实物在同一保管地点的正时长共处时段(`contacts`)。
- **到货核对**:上传 1~60 条到货实扫与 1~60 条期望明细行,解析、去重并在名额约束下做全局最优核销,返回每条扫描的分配行、缺额(行剩余数量)与多余扫描。

### 重放内核入口与接口策略差异

内核类 `ReplayKernel(config)` 在 `app/replay.py`,逐事件调用(可随时中断、继续):

```python
from app.replay import ReplayKernel, ReplayConfig

kernel = ReplayKernel(ReplayConfig.trace())   # 或 .packing() / .custody()
failure = kernel.transfer(index, item_key, from_location, to_location, precheck=None)
failure = kernel.pack(index, parent_key, child_key)
failure = kernel.unpack(index, parent_key, child_key)
# 状态:kernel.nodes(持有人 / locations / 成功下标)、kernel.parent_of、kernel.involved
```

内核**不统一成最严模式**,三套配置保留各接口原有语义差异:

| 语义 | 独立装拆 `packing()` | 独立交接 `custody()` | 地点追溯 `trace()` |
| --- | --- | --- | --- |
| 实物来源 | 成功装入时登记新实物 | 首次成功交接时建立 | 以**库存扫描**预注册为基准 |
| 持有人要求 | 无(装 / 拆都不要求) | 交接自身维护 | 装入双方须**已有相同持有人** |
| 批号 / 有效期基准 | 不校验 | **首次成功事件**(提交前回调比对,含字段缺失) | 库存扫描 |
| 交接作用范围 | — | 被交接实物本身 | 仅无父件根件;**根件交接带动全部后代** |
| 取出 | 双方须为成功涉及实物,须当前直接父子边 | — | 子件取出后**保留当时持有人**,不重复记地点 |
| 失败命令 | 不改变任何关系、不加入成功涉及集合 | 不改持有人 / 成功下标,基准不变 | 不留父子边 / 持有人 / 成功下标 / 地点历史半次更新 |

各旧接口的方法、路径、请求与响应字段、状态码、错误下标与排序保持不变:
`packing.replay_packing`(隔离计划继续复用)返回 `(parent_of, involved, errors)`,
`audit_packing` / `audit_custody` / `trace_locations` 负责把内核的机器可读失败码
(`ReplayFailure.code`)整理为各接口原有的中文 `reason` 文案。

## 快速启动

仓库自带一份**本地演示用**信任文件 `trust/recall-keys.json`(由脚本生成,见下),直接启动即可体验全部接口:

```bash
docker compose up --build
```

> 容器把宿主机的 **`trust/` 整个目录只读挂载**到 `/srv/trust`(而**不是逐个文件绑定**):
> 信任文件 `recall-keys.json` 与单包撤销名单 `revoked-packages.json` 都在其中。逐个文件绑定会把
> 挂载钉死在宿主机**当前 inode** 上——宿主机用 `os.replace`/`rename(2)` 原子替换文件后,容器仍解析到
> 旧 inode,读到旧名单;绑定**目录**时容器每次按路径名重新解析,原子替换后下一次请求即读到新文件,
> 无需重启容器。撤销名单**每次请求读盘一次**;`trust/` 目录只放可暴露给容器的信任文件
> (样例私钥在 `examples/sample-private-keys.json`,**不在**挂载目录内)。生产环境请用自己的信任文件与
> 撤销名单替换样例。

服务监听 **8000** 端口:

- 召回核验:`POST http://localhost:8000/v1/recall/check`
- 签名召回核验:`POST http://localhost:8000/v1/recall/check-signed`
- 召回规则关系分析:`POST http://localhost:8000/v1/recall/rule-relations`
- 交接审计:`POST http://localhost:8000/v1/custody/audit`
- 交接日志最少剔除:`POST http://localhost:8000/v1/custody/trim`
- 装拆包装审计:`POST http://localhost:8000/v1/packing/audit`
- 召回隔离计划:`POST http://localhost:8000/v1/recall/isolation-plan`
- 召回实物地点追溯:`POST http://localhost:8000/v1/recall/location-trace`
- 到货核对:`POST http://localhost:8000/v1/receiving/reconcile`
- 交互文档:`http://localhost:8000/docs`
- 健康检查:`GET http://localhost:8000/healthz`

本地开发(无需 Docker):

```bash
pip install -r requirements.txt
# 信任文件路径可用环境变量覆盖,默认 trust/recall-keys.json(相对工作目录)
export RECALL_TRUST_FILE=trust/recall-keys.json
# 单包撤销名单(可选):不设置或留空则不启用撤销,check-signed 保持旧行为
export RECALL_REVOKED_FILE=trust/revoked-packages.json
uvicorn app.main:app --reload --port 8000
```

### 生成本地样例(密钥 / 信任文件 / 请求体)

```bash
python3 scripts/generate_sample_keys.py            # 生成新 Ed25519 密钥并覆盖样例
python3 scripts/generate_sample_keys.py --force --now 1790000000   # 固定 now 的确定性样例
```

产物:

| 文件 | 说明 |
| --- | --- |
| `trust/recall-keys.json` | 本地信任文件:`kid`、Ed25519 公钥(32 字节裸公钥的无填充 base64url)、授权 GTIN 前缀 |
| `examples/sample-private-keys.json` | **样例私钥**,仅用于本地重新签发请求;服务端不读取、不挂载进容器,勿用于生产 |
| `examples/sample-check-signed.json` | 可直接 `--data @` 提交的请求体样例(含 `scans`、`jws`、`now`) |

常用选项:`--kid`、`--prefix 0950600`(可重复,多个授权前缀)、`--gtin`、`--now`、`--nbf-offset`、`--exp-offset`。JWS 时间窗口随生成时刻而定,过期后重跑脚本即可。

运行测试:

```bash
pip install pytest httpx
pytest
```

## 接口说明

### `POST /v1/recall/check`

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | 1~100 条扫描串 |
| `rules` | `object[]` | 1~100 条规则,`id` 必须唯一 |

规则字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 规则 ID,1~64 字符,请求内唯一 |
| `gtin` | 是 | 14 位数字,须通过 GS1 模 10 校验位 |
| `batch` | 否 | 批号,1~20 位可打印 ASCII;附带时扫描串须含 AI 10 且相等 |
| `serial` | 否 | 序列号,1~20 位可打印 ASCII;附带时扫描串须含 AI 21 且相等 |
| `date_from` / `date_to` | 否 | ISO 日期(`YYYY-MM-DD`)闭区间;附带时扫描串须含 AI 17 且日期落在区间内(含端点) |

规则不合法(GTIN 校验位错误、ID 重复、数量超限、日期区间倒置等)时整个请求返回 **422**。

### 扫描串语法

- 可带符号标识符前缀 `]d2`(GS1 DataMatrix)。
- FNC1 为 **ASCII 29**(`\x1d`,GS),用作字段分隔符。
- 支持的 AI,可按**任意顺序**出现,每种**至多一次**:

| AI | 含义 | 格式 |
| --- | --- | --- |
| `01` | GTIN | 定长 14 位数字,模 10 校验位,**必填** |
| `17` | 日期 | 定长 6 位 `YYMMDD`,年按 2000~2099 解释,日 `00` 取当月末日 |
| `10` | 批号 | 变长 1~20 位可打印 ASCII,**仅由 FNC1 或串尾结束** |
| `21` | 序列号 | 变长 1~20 位可打印 ASCII,**仅由 FNC1 或串尾结束** |

定长字段后的 FNC1 可省略;变长字段若后面还有 AI,必须用 FNC1 分隔,否则后续内容会被吞进该字段。

### 响应

```json
{
  "results": [
    {
      "index": 0,
      "ok": true,
      "fields": {
        "gtin": "09506001123454",
        "batch": "LOT1",
        "serial": "SN9",
        "date": "2025-12-31"
      },
      "matches": ["recall-2026-001"],
      "error": null
    },
    {
      "index": 1,
      "ok": false,
      "fields": null,
      "matches": [],
      "error": { "offset": 16, "reason": "未知 AI: '99'" }
    }
  ]
}
```

- `results` 与 `scans` 一一对应,`index` 为扫描串下标。
- `ok=true`:`fields` 为解析出的字段(`date` 为 AI 17 解析结果,ISO 格式),`matches` 为命中的规则 ID,**按请求中规则的顺序排列**。
- `ok=false`:解析失败,`error.offset` 为出错位置(**相对原始扫描串、含 `]d2` 前缀,从 0 计**),`error.reason` 为原因(未知 AI、非法字符、长度不足 / 超长、分隔符误用、校验位错误、日期非法、缺少 AI 01 等)。**单条失败不影响其余条目**。

### 示例

```bash
curl -s http://localhost:8000/v1/recall/check \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": ["]d201095060011234541725123110LOT1\u001d21SN9"],
    "rules": [
      {"id": "recall-2026-001", "gtin": "09506001123454", "batch": "LOT1",
       "date_from": "2025-01-01", "date_to": "2026-12-31"}
    ]
  }'
```

> JSON 中 FNC1 写作 `\u001d`;shell 里也可用 `$'...\x1d...'` 构造。

### `POST /v1/recall/check-signed`

与召回核验相同的扫码结果,但召回规则由 **compact JWS** 携带并经过 Ed25519 验签;服务不接受请求体明文规则。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | 1~100 条扫描串,语法同召回核验 |
| `jws` | `string` | compact JWS:`BASE64URL(header).BASE64URL(payload).BASE64URL(signature)`,**无填充** base64url |
| `now` | `integer` | 当前时间,Unix 秒(**须为整数,布尔值拒绝**) |

JWS **头部**解码后是 JSON 对象,**仅含**两个键:

| 键 | 值 |
| --- | --- |
| `alg` | `"EdDSA"`(Ed25519) |
| `kid` | 非空字符串,须存在于本地信任文件 |

JWS **载荷**解码后是 JSON 对象,**仅含**三个键:

| 键 | 值 |
| --- | --- |
| `nbf` | 整数 Unix 秒,生效时间 |
| `exp` | 整数 Unix 秒,失效时间 |
| `rules` | 非空数组(1~100 条),规则格式与召回核验**完全一致**(GTIN 模 10、`id` 唯一、日期区间等) |

**签名**:对 JWS 的**原始前两段以点连接后的字节**(`header.payload`,不解码、不规范化)用 kid 对应私钥做 Ed25519 签名,结果以无填充 base64url 作为第三段。

**JSON 去重**:头部与载荷的 JSON 在**任一层**(含规则对象内部)出现重复键都按格式非法拒绝。

核验顺序(全部通过才执行扫码匹配):

1. **格式**:三段齐全非空、均为无填充 base64url;头部 / 载荷为 UTF-8 JSON,无重复键、无额外键、类型正确;`rules` 复用现有规则校验。
2. **取钥**:`kid` 从**本地信任文件**(`RECALL_TRUST_FILE`,默认 `trust/recall-keys.json`;容器内 `/srv/trust/recall-keys.json`)查公钥与授权 GTIN 前缀,**不联网取钥**。
3. **验签**:Ed25519 验证失败即拒绝。
4. **时间窗**:须 `nbf ≤ now < exp`(下边界闭、上边界开)。
5. **授权前缀**:载荷中**每条**规则的 14 位 GTIN 都须**至少匹配一个**该 kid 的授权前缀(1~14 位数字,按 GTIN 字符串前缀匹配;14 位前缀即精确相等)。
6. **单包撤销**(仅当配置 `RECALL_REVOKED_FILE` 时):包标识 `SHA-256(header.payload 原始两段 ASCII 字节)`命中撤销名单即拒绝,即使包仍在有效期内;详见下文。

状态码:

| 状态码 | 情形 |
| --- | --- |
| **200** | 全部通过,返回 `kid` 与原扫码核验结果(逐条解析错误仍以 `ok:false` 内联返回,不影响其余条目) |
| **422** | 请求体非法;JWS 段数 / 空段 / base64url / JSON / 重复键 / 头部或载荷字段错误;载荷规则非法(GTIN 校验位、ID 重复、日期倒置等) |
| **403** | 未知 `kid`、签名验证失败、时间窗越期(`nbf > now` 或 `now ≥ exp`)、任一规则 GTIN 未获前缀授权、规则包命中本地撤销名单 |
| **500** | 本地信任文件缺失或内容非法;**已配置**的撤销名单文件不可读、结构或元素非法(服务端配置问题,失败即拒绝,不沿用旧名单放行) |

422 / 403 均**不返回任何匹配结果**。

响应:

```json
{
  "kid": "recall-key-001",
  "results": [
    {
      "index": 0,
      "ok": true,
      "fields": {
        "gtin": "09506001123454",
        "batch": "LOT1",
        "serial": "SN9",
        "date": "2025-12-31"
      },
      "matches": ["recall-2026-001"],
      "error": null
    }
  ]
}
```

`results` 结构与 `/v1/recall/check` 完全一致。

信任文件格式(本地 JSON,`kid` 唯一,前缀为 1~14 位数字的非空数组):

```json
{
  "keys": [
    {
      "kid": "recall-key-001",
      "public_key": "BASE64URL(32字节Ed25519裸公钥)",
      "gtin_prefixes": ["0950600", "0859"]
    }
  ]
}
```

示例(请求体由 `scripts/generate_sample_keys.py` 生成于 `examples/sample-check-signed.json`):

```bash
curl -s http://localhost:8000/v1/recall/check-signed \
  -H 'Content-Type: application/json' \
  --data @examples/sample-check-signed.json
```

#### 单包撤销(本地撤销名单)

在验签、时间窗与 GTIN 授权全部通过之后,还可对**指定的单个规则包**做撤销:

- 由环境变量 **`RECALL_REVOKED_FILE`** 指向一份本地 JSON 名单(默认不设置 / 空 = 不启用,接口保持旧行为);容器内为 `/srv/trust/revoked-packages.json`。Compose 把宿主机 **`trust/` 目录只读挂载**到 `/srv/trust`(不是逐个文件绑定),因此宿主机原子替换该文件后容器立即按路径名读到新 inode。
- 名单**仅含**一个 `revoked` 数组,元素为 **64 位小写十六进制** SHA-256 摘要,**允许重复**(重复与单条等价);空数组表示当前没有任何包被撤销。
- **包标识**取 JWS **原始前两段以点连接的 ASCII 字节**的 SHA-256(正是 Ed25519 的签名输入),与第三段签名无关——因此**签名段编码变化不影响标识**,重传同一头 / 载荷的包仍会命中;而载荷(规则 / 时间窗)或头部任一字节不同即为另一个包。撤销**只命中该包本身**,不连带撤销同一 `kid` 签发的其他包。
- 服务对每次 check-signed 请求都重新读盘**一次**,不做缓存:在宿主机上**原子替换**名单文件后,**下一次请求即生效**,无需重启容器。
- 已配置名单但文件**不可读、JSON 非法、结构或元素非法**时返回 **500** 且不返回匹配结果(**失败即拒绝**,不会沿用旧名单内容放行)。

名单格式:

```json
{
  "revoked": [
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  ]
}
```

下面给出一段**可直接执行**的端到端示例(配合 `docker compose up --build` 在 8000 端口运行的服务),完整覆盖:从实际样例计算摘要 → 创建非 root 可读的更新文件 → 原子替换 → HTTP 调用(200/403/同 kid 兄弟包不受影响)→ 非法替换返回 500 → 换回空名单恢复 200。

**① 从实际样例请求计算包标识**(取前两段,不看签名段):

```bash
DIGEST=$(python3 -c '
import hashlib, json
jws = json.load(open("examples/sample-check-signed.json"))["jws"]
print(hashlib.sha256(jws.rsplit(".", 1)[0].encode("ascii")).hexdigest())
')
echo "$DIGEST"   # 对当前提交的样例:f00c691498058b5ece3d288b721ee86ced8417343a4011d42c1396889745c335
```

等价的纯 shell 写法(`jws=header.payload.signature`,只取前两段):

```bash
jws=$(python3 -c 'import json;print(json.load(open("examples/sample-check-signed.json"))["jws"])')
printf '%s' "${jws%.*}" | sha256sum   # 输出的 64 位十六进制即包标识
```

**② 先确认空名单下有效包返回 200**(初始 `trust/revoked-packages.json` 即 `{"revoked": []}`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/recall/check-signed \
  -H 'Content-Type: application/json' --data @examples/sample-check-signed.json
# 200
```

**③ 创建非 root 可读的更新文件并原子替换**:新文件权限显式置为 `0644`
(容器以非 root uid 10001 运行,须对其可读),先写同目录临时文件再 `os.replace`
(`rename(2)`)原子改名,容器绝不会读到写一半的文件:

```bash
python3 - "$DIGEST" <<'PY'
import json, os, sys
digest = sys.argv[1]
path = "trust/revoked-packages.json"
tmp = os.path.join(os.path.dirname(path), ".revoked-packages.updating.tmp")
# 0644:属主可写、所有人(含容器内非 root uid)可读
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump({"revoked": [digest]}, fh)
    fh.write("\n")
os.replace(tmp, path)          # 原子替换;容器下次按路径名打开即见新 inode
os.chmod(path, 0o644)
PY
```

**④ 下一次请求(不重启容器)即返回 403 且无匹配结果**:

```bash
curl -s -i http://localhost:8000/v1/recall/check-signed \
  -H 'Content-Type: application/json' --data @examples/sample-check-signed.json
# HTTP/1.1 403 Forbidden
# {"detail":"规则包已被撤销: f00c6914...c335"}   ← 无 kid / results 字段
```

撤销**只命中该包本身**,**同一 `kid` 签发的其他包仍可正常核验**(200)。用样例私钥
(`examples/sample-private-keys.json`,仅本地联调)再签一个同 kid、不同载荷的包即可验证:

```bash
python3 - <<'PY'
import base64, json, os, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sample = json.load(open("examples/sample-check-signed.json"))
priv_b64 = json.load(open("examples/sample-private-keys.json"))["keys"][0]["private_key"]
pk = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(priv_b64 + "=="))
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

now = sample["now"]                                   # 沿用样例的确定性 now
kid = json.loads(base64.urlsafe_b64decode(             # 从样例 JWS 头部取 kid
    sample["jws"].split(".")[0] + "=="))["kid"]
FNC1 = "\x1d"
gtin2 = "09506001123461"                              # 同授权前缀 0950600
scan2 = "]d2" + "01" + gtin2 + "17" + "251231" + "10" + "LOT1" + FNC1 + "21SN9"
rule = {"id": "recall-2026-002", "gtin": gtin2, "batch": "LOT1",
        "date_from": "2025-01-01", "date_to": "2026-12-31"}
header = {"alg": "EdDSA", "kid": kid}
payload = {"nbf": now - 300, "exp": now + 86400, "rules": [rule]}
h = b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
p = b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
jws2 = f"{h}.{p}." + b64(pk.sign(f"{h}.{p}".encode("ascii")))

req = urllib.request.Request(
    "http://localhost:8000/v1/recall/check-signed",
    data=json.dumps({"scans": [scan2], "jws": jws2, "now": now}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    body = json.load(r)
print(r.status, body["results"][0]["matches"])        # 200 ['recall-2026-002']
PY
```

**⑤ 换回空名单后立即恢复 200**:

```bash
python3 - <<'PY'
import json, os
path = "trust/revoked-packages.json"
tmp = os.path.join(os.path.dirname(path), ".revoked-packages.updating.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump({"revoked": []}, fh)
    fh.write("\n")
os.replace(tmp, path)
os.chmod(path, 0o644)
PY
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/recall/check-signed \
  -H 'Content-Type: application/json' --data @examples/sample-check-signed.json
# 200
```

**⑥ 非法替换一律 500 且无匹配结果,绝不回退旧内容**。以当前空名单为基线(合法包本应 200),
把名单替换成不可读 / JSON 非法 / 结构或元素非法的内容后,响应必须是 **500**;若错误地沿用了
旧(空)名单就会得到 200。逐项演示:

```bash
# 不可读:替换为 0000,容器内非 root uid 无法打开
printf '{"revoked": []}' > trust/.revoked-packages.updating.tmp
chmod 000 trust/.revoked-packages.updating.tmp
mv -f trust/.revoked-packages.updating.tmp trust/revoked-packages.json
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/recall/check-signed \
  -H 'Content-Type: application/json' --data @examples/sample-check-signed.json   # 500

# JSON / 结构 / 元素非法(同样原子替换后调用,均为 500):
for bad in 'not json' '[]' '{}' '{"revoked": "x"}' '{"revoked": [1]}' \
           '{"revoked": ["ABC"]}' '{"revoked": [],"extra": 1}'; do
  printf '%s' "$bad" > trust/.revoked-packages.updating.tmp
  chmod 644 trust/.revoked-packages.updating.tmp
  mv -f trust/.revoked-packages.updating.tmp trust/revoked-packages.json
  curl -s -o /dev/null -w "$bad => %{http_code}\n" http://localhost:8000/v1/recall/check-signed \
    -H 'Content-Type: application/json' --data @examples/sample-check-signed.json   # 均 500
done

# 恢复合法空名单(别忘了把权限改回 644)
printf '{\n  "revoked": []\n}\n' > trust/.revoked-packages.updating.tmp
chmod 644 trust/.revoked-packages.updating.tmp
mv -f trust/.revoked-packages.updating.tmp trust/revoked-packages.json
```

> 为什么必须**目录挂载**:Linux 下逐个文件的绑定挂载(`./trust/x.json:/srv/trust/x.json`)
> 关联的是挂载建立那一刻的**inode**;宿主机用 `os.replace` 原子替换只是让**目录项**指向新
> inode,容器里的旧绑定仍解析到旧 inode,于是持续读到旧文件。绑定**目录**
> (`./trust:/srv/trust:ro`)后,容器每次 `open(path)` 都重新经目录项解析,原子改名即时可见。

#### 用真实容器一键覆盖上述全过程

`scripts/verify_revocation_container.sh` 用 **`docker compose up --build`** 起真实容器,自动完成
构建 → 健康就绪 → 从实际样例计算摘要 → 同目录原子替换(0644 非 root 可读)→ 200/403/同 kid 兄弟包
200/换回 200 → 不可读与各类非法名单 500 → 恢复,并核对容器以非 root 运行、`/srv/trust` 只读、
样例私钥不在容器内、全程容器 ID 不变(即热更新无需重启);结束时自动 `compose down` 并恢复宿主机
名单原文件。

```bash
scripts/verify_revocation_container.sh
# ... ALL CHECKS PASSED ✅
```

### `POST /v1/recall/rule-relations`

召回规则上线前的静态检查:不必提供扫描串,直接比较规则两两的命中集合,找出可能命中同一扫描串的规则对。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `rules` | `object[]` | **2~100** 条规则,格式同召回核验,`id` 必须唯一 |

处理规则:

1. **不可能命中的规则**:AI 17 只能表达 2000~2099 年的合法日期,规则的日期区间夹取到该范围后为空(如 `date_to` 早于 2000-01-01、或 `date_from` 晚于 2099-12-31)时,该规则不可能命中任何合法扫描串,其 ID 按**输入顺序**列入 `impossible_rule_ids`,不参与两两比较。
2. **规则对比较**:其余规则两两比较命中集合(匹配语义同召回核验:GTIN 相等为前提;批号 / 序列号附带时须相等;日期区间须相交),仅返回**有交集**的规则对,按 `(left_index, right_index)` 输入下标排序。
3. 每对附一条**同时命中两者**的合法 GS1 扫描串 `witness`:批号、序列号只在约束需要时出现;日期有约束时取**交集内最早一天**;变长字段(AI 10/21)后仍有 AI 时加 FNC1。

响应:

```json
{
  "impossible_rule_ids": ["recall-legacy"],
  "pairs": [
    {
      "left_index": 0,
      "right_index": 1,
      "left_id": "recall-all",
      "right_id": "recall-lot1",
      "relation": "left_contains_right",
      "witness": "010950600112345410LOT1"
    }
  ]
}
```

`relation` 取值:

| 值 | 含义 |
| --- | --- |
| `equivalent` | 等价:两条规则的命中集合相同 |
| `left_contains_right` | 左包含右:右规则(下标较大者)的命中集合是左规则的真子集 |
| `right_contains_left` | 右包含左:左规则的命中集合是右规则的真子集 |
| `partial_overlap` | 部分相交:有交集但互不包含 |

规则不合法(数量不足 2 条或超限、GTIN 校验位错误、ID 重复、日期区间倒置等)时整个请求返回 **422**。

示例:

```bash
curl -s http://localhost:8000/v1/recall/rule-relations \
  -H 'Content-Type: application/json' \
  -d '{
    "rules": [
      {"id": "recall-all",      "gtin": "09506001123454"},
      {"id": "recall-lot1",     "gtin": "09506001123454", "batch": "LOT1"},
      {"id": "recall-lot1-exp", "gtin": "09506001123454", "batch": "LOT1",
       "date_from": "2025-01-01", "date_to": "2025-06-30"},
      {"id": "recall-legacy",   "gtin": "09506001123454", "date_to": "1999-12-31"}
    ]
  }'
```

响应中 `recall-legacy` 因日期区间完全在 2000 年之前而被列入 `impossible_rule_ids`;其余三条规则两两构成 `left_contains_right` 关系,`witness` 如 `01095060011234541725010110LOT1`(含 AI 17 2025-01-01 与批号 LOT1,同时命中 `recall-all` 与 `recall-lot1-exp`)。

### `POST /v1/custody/audit`

审计一批实物交接事件:按发生时间重放,维护每件实物(**GTIN + AI 21 序列号**)的持有人,返回每件实物的成功递交结果与失败事件的原因。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `events` | `object[]` | 1~300 条交接事件 |

事件字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scan` | 是 | GS1 扫描串,语法同召回核验;须含 AI 01 与 AI 21(序列号) |
| `occurred_at` | 是 | ISO 8601 时间,**须带时区**(如 `2026-09-01T08:30:00+08:00`) |
| `from_location` | 是(可为 `null`) | 交出方;实物的首次事件须为 `null` |
| `to_location` | 是 | 接收方,**非空**字符串 |

处理规则:

1. 事件按 `occurred_at` **转 UTC 后升序**处理;同一时刻依输入顺序。
2. 扫描非法或缺少 AI 21 序列号的事件按原下标报错,不参与流转。
3. 每件实物的**首次成功事件** `from_location` 须为 `null`;后续事件 `from_location` 须等于**上次成功事件**的 `to_location`。
4. 批号(AI 10)、有效期(AI 17)须与首次成功扫描一致(**含字段缺失**:首次没有该字段时,后续也不能有),否则记录冲突且**不改变持有人**。
5. 只有成功事件参与流转;失败事件不影响后续事件的判定基准。

响应:

```json
{
  "items": [
    {
      "gtin": "09506001123454",
      "serial": "SN9",
      "events": [1, 0],
      "holder": "WH-B"
    }
  ],
  "errors": [
    { "index": 2, "reason": "from_location 须等于上次成功事件的 to_location 'WH-B',实际为 'WH-X'" }
  ]
}
```

- `items` 按 **GTIN、序列号**排序;`events` 为该实物成功事件的原始下标(按处理顺序,即时间升序),`holder` 为最终持有人(最后一次成功事件的 `to_location`)。
- `errors` 按**原始下标**排序,`reason` 说明失败原因(扫描非法、缺少序列号、首次事件 `from_location` 非 `null`、交接链断裂、批号 / 有效期冲突)。
- 请求体不合法(事件数量超限、`occurred_at` 缺时区、`to_location` 为空、字段缺失或多余等)时整个请求返回 **422**。

示例:

```bash
curl -s http://localhost:8000/v1/custody/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T09:00:00+08:00",
       "from_location": null, "to_location": "WH-A"},
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T10:00:00+08:00",
       "from_location": "WH-A", "to_location": "WH-B"}
    ]
  }'
```

### `POST /v1/custody/trim`

为交接日志生成**最少剔除方案**:在现有交接事件中选择一个保留子序列,使剔除事件总数最少;与交接审计使用**同一套事件格式与 GS1 解析**(旧接口 `/v1/custody/audit` 保持不变)。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `events` | `object[]` | **1~300** 条交接事件,字段同交接审计(`scan`、`occurred_at`、`from_location`、`to_location`) |
| `locked_indices` | `integer[]` | 可选,锁定事件的**原下标**(从 0 计);重复或越界时整个请求返回 **422** |

处理规则:

1. 事件按 `occurred_at` **转 UTC 后升序**处理;同一时刻依输入顺序;实物以 **GTIN + AI 21 序列号**标识。
2. **扫描非法或缺 AI 21 序列号**的事件必须剔除;若该事件被 `locked_indices` 锁定,整单返回 **422**。
3. 每件实物的保留事件须构成合法交接链:
   - 首条保留事件 `from_location` 为 `null`;
   - 后续事件 `from_location` 等于**上一条保留事件**的 `to_location`;
   - 批号(AI 10)、有效期(AI 17)**含字段缺失**须与该实物首条保留事件一致。
4. `locked_indices` 指向的事件**必须保留**;锁定事件之间无法同时保留(交接链断裂、批号 / 有效期冲突等)时整单返回 **422**。
5. 优化目标:**剔除总数最少**(即保留总数最多);仍并列时,把保留下标**按处理顺序排列**成序列,取该序列**字典序最小**的方案。注意比较的是各实物合并后的全局序列(原下标顺序与 UTC 处理顺序可能交错),并非逐实物各自字典序最小。
6. 未保留任何事件的实物不出现在结果中。

响应:

```json
{
  "kept_indices": [0, 2],
  "removed_indices": [1],
  "items": [
    {"gtin": "09506001123454", "serial": "SN9", "holder": "WH-B"}
  ]
}
```

- `kept_indices`:保留事件的**原下标**,按**处理顺序**(UTC 升序、同刻输入顺序)排列。
- `removed_indices`:剔除事件的原下标,按**原下标升序**排列(含扫描非法 / 缺序列号事件)。
- `items`:保留实物与**最终持有人**(最后一条保留事件的 `to_location`),按 **GTIN、序列号**排序;未保留任何事件的实物不列出。

示例(下标 1 的事件交接链断裂被剔除;锁定下标 2 的合法事件必须保留):

```bash
curl -s http://localhost:8000/v1/custody/trim \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T00:00:00Z",
       "from_location": null, "to_location": "WH-A"},
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T01:00:00Z",
       "from_location": "WH-Z", "to_location": "WH-X"},
      {"scan": "01095060011234541725123110LOT1\u001d21SN9",
       "occurred_at": "2026-09-01T02:00:00Z",
       "from_location": "WH-A", "to_location": "WH-B"}
    ],
    "locked_indices": [2]
  }'
```

响应:`kept_indices` 为 `[0, 2]`,`removed_indices` 为 `[1]`,实物 `SN9` 的最终持有人为 `WH-B`。

### `POST /v1/packing/audit`

审计一批**按输入顺序执行**的装入 / 取出操作:父件与子件均以 GS1 扫描串给出(语法同召回核验),以 **GTIN + AI 21 序列号**唯一识别实物;重放后返回成功操作涉及的全部实物及其当前包装关系,以及失败操作的原因。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `operations` | `object[]` | 1~300 条装/拆操作,**严格按下标顺序执行** |

操作字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `action` | 是 | `"pack"`(装入)或 `"unpack"`(取出) |
| `parent_scan` | 是 | 父件 GS1 扫描串,须含 AI 01 与 AI 21(序列号) |
| `child_scan` | 是 | 子件 GS1 扫描串,须含 AI 01 与 AI 21(序列号) |

处理规则:

1. 两端扫描串复用同一套 GS1 解析;任一端非法、或缺少 AI 21 序列号,该操作按**原下标**报错且不改变任何关系。
2. **装入(pack)**:子件须**尚无父件**;且不能是父件本身,也不能是父件的祖先(防止自装 / 形成循环包装)。父件若此前未出现,记为无父件的新实物。
3. **取出(unpack)**:父件、子件均须为已知实物,且指定的父件必须是子件的**当前直接父件**(祖父件不可直接取出孙子件)。
4. **失败操作不改变包装关系**,后续操作照常处理;只有成功操作涉及的实物才会出现在响应中。

响应:

```json
{
  "items": [
    {
      "gtin": "09506001123454",
      "serial": "PALLET-A",
      "parent": null,
      "children": [{ "gtin": "09506001123461", "serial": "CASE-1" }],
      "root": { "gtin": "09506001123454", "serial": "PALLET-A" }
    },
    {
      "gtin": "09506001123461",
      "serial": "CASE-1",
      "parent": { "gtin": "09506001123454", "serial": "PALLET-A" },
      "children": [],
      "root": { "gtin": "09506001123454", "serial": "PALLET-A" }
    }
  ],
  "errors": [
    { "index": 1, "reason": "取出失败:指定的父件实物 (GTIN 09506001123478, 序列号 'PALLET-X')此前未出现在任何成功操作中" }
  ]
}
```

- `items` 为**成功操作涉及的全部实物**,按 **GTIN、序列号**排序。
  - `parent`:当前直接父件引用;无父件时为 `null`。
  - `children`:当前直接子件引用列表,按 **GTIN、序列号**同样排序。
  - `root`:沿父链向上找到的**最外层容器**引用;无父件时即为实物**自身**。
  - 引用对象形如 `{"gtin": "...", "serial": "..."}`。
- `errors` 按**原始下标**排序,`reason` 说明失败原因(父件 / 子件扫描非法、缺少序列号、子件已有父件、子件即父件本身、装入祖先成环、取出未指定当前直接父件等)。
- 请求体不合法(操作数量超限、`action` 取值非法、字段缺失或多余等)时整个请求返回 **422**。

示例:

```bash
curl -s http://localhost:8000/v1/packing/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "operations": [
      {"action": "pack",
       "parent_scan": "010950600112345421PALLET-A",
       "child_scan":  "010950600112346121CASE-1"},
      {"action": "unpack",
       "parent_scan": "010950600112347821PALLET-X",
       "child_scan":  "010950600112346121CASE-1"}
    ]
  }'
```

上例第 0 条装入成功;第 1 条取出因指定的父件不是当前直接父件而失败(且不改变关系)。

### `POST /v1/recall/isolation-plan`

生成召回隔离计划:库存扫描命中召回规则后,把命中实物沿**最终包装父链**归并到**最高层已封签祖先**(含实物自身),输出待隔离的单元。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | 1~100 条库存扫描串,须含 AI 21(序列号) |
| `rules` | `object[]` | 1~100 条召回规则,格式同召回核验,`id` 必须唯一 |
| `operations` | `object[]` | 1~300 条装/拆操作,格式同装拆包装审计,**严格按下标顺序执行** |
| `sealed_scans` | `string[]` | 0~300 条已封签实物扫描串,须含 AI 21(序列号) |

处理规则:

1. **封签扫描**(`sealed_scans`)须全部合法、含 AI 21 且 GTIN+序列号不重复,否则整个请求返回 **422**。
2. **库存扫描**(`scans`)逐条解析,须含 AI 21;非法或缺序列号的扫描按**原下标**记入 `errors` 并跳过,不影响其余条目。**合法**扫描的 GTIN+序列号重复时,整个请求返回 **422**。
3. **装拆操作**(`operations`)复用装拆包装审计的重放逻辑:失败操作**不改变包装关系**,按**原下标**记入 `operation_errors`。
4. 合法库存扫描复用召回核验的规则匹配;**未命中任何规则的扫描不产出单元**。
5. 对每件命中召回的实物,沿重放后的**最终父链**向上选**最高层已封签祖先**(含实物自身)作为隔离单元;父链上没有已封签祖先时,单元即为该实物自身。
6. 归入同一单元的命中扫描合并:`scan_indices` 为命中库存扫描的原始下标(升序),`rule_ids` 为命中的规则 ID(**去重后保持规则输入顺序**)。

响应:

```json
{
  "units": [
    {
      "gtin": "09506001123454",
      "serial": "PALLET-A",
      "scan_indices": [0, 2],
      "rule_ids": ["recall-2026-001"]
    }
  ],
  "errors": [
    { "index": 1, "reason": "扫描串缺少 AI 21 序列号,无法识别实物" }
  ],
  "operation_errors": [
    { "index": 2, "reason": "装入失败:子件实物 (GTIN 09506001123461, 序列号 'CASE-1')已有直接父件实物 (GTIN 09506001123454, 序列号 'PALLET-A'),不能重复装入" }
  ]
}
```

- `units` 按 **GTIN、序列号**排序;`errors`(库存扫描)与 `operation_errors`(装拆操作)均按**原始下标**排序。
- 请求体不合法(数量超限、规则 ID 重复、字段缺失或多余等),或封签扫描非法 / 缺序列号 / 重复,或合法库存扫描重复时,整个请求返回 **422**。

示例:库存中 `CASE-1`(下标 0)与 `CASE-2`(下标 2)都装在已封签的托盘 `PALLET-A` 里,且都命中召回规则;下标 1 的库存扫描缺序列号被跳过;下标 2 的装入操作因重复装入而失败。

```bash
curl -s http://localhost:8000/v1/recall/isolation-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": [
      "010950600112346121CASE-1",
      "010950600112345417251231",
      "010950600112347821CASE-2"
    ],
    "rules": [
      {"id": "recall-2026-001", "gtin": "09506001123461"},
      {"id": "recall-2026-002", "gtin": "09506001123478"}
    ],
    "operations": [
      {"action": "pack",
       "parent_scan": "010950600112345421PALLET-A",
       "child_scan":  "010950600112346121CASE-1"},
      {"action": "pack",
       "parent_scan": "010950600112345421PALLET-A",
       "child_scan":  "010950600112347821CASE-2"},
      {"action": "pack",
       "parent_scan": "010950600112345421PALLET-A",
       "child_scan":  "010950600112346121CASE-1"}
    ],
    "sealed_scans": ["010950600112345421PALLET-A"]
  }'
```

响应中两个命中实物被归并到同一个隔离单元 `PALLET-A`:`units[0].scan_indices` 为 `[0, 2]`,`rule_ids` 为 `["recall-2026-001", "recall-2026-002"]`。

### `POST /v1/recall/location-trace`

追溯召回实物经过的**保管地点**:以库存扫描确立实物身份与批号 / 有效期基准,重放一批带时区的**交接**与**装入 / 取出**事件,维护包装树与持有人;仅对命中召回规则的库存实物,返回其按**首次到达顺序去重**的地点列表。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | **1~100** 条库存扫描串,语法同召回核验,须含 AI 21 |
| `rules` | `object[]` | **1~100** 条召回规则,格式同召回核验,`id` 必须唯一 |
| `events` | `object[]` | **1~200** 条交接 / 装拆事件,以 `action` 区分;`occurred_at` 均须**带时区** |
| `contact_until` | `string` | 可选;**带时区**的 ISO 8601 时间,提供时须**不早于任何事件时间**(否则 422);提供后响应附带 `contacts` |

**库存扫描**须全部合法、含 AI 21,且 GTIN+序列号在请求内唯一(批号 / 有效期不同也算同一实物),否则整个请求返回 **422**。

交接事件(`"action": "transfer"`):

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scan` | 是 | GS1 扫描串,须含 AI 01 与 AI 21 |
| `occurred_at` | 是 | ISO 8601 时间,须带时区 |
| `from_location` | 是(可为 `null`) | 交出方;实物的首次成功交接须为 `null` |
| `to_location` | 是 | 接收方,非空字符串 |

装 / 拆事件(`"action": "pack"` / `"unpack"`):

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `parent_scan` | 是 | 父件 GS1 扫描串,须含 AI 21 |
| `child_scan` | 是 | 子件 GS1 扫描串,须含 AI 21 |
| `occurred_at` | 是 | ISO 8601 时间,须带时区 |

事件重放规则:

1. 事件按 `occurred_at` **转 UTC 后升序**处理;同一时刻依**输入顺序**。
2. 事件扫描**非法、缺 AI 21、引用了库存中不存在的实物**,或**批号(AI 10)/ 有效期(AI 17)与库存扫描不一致(含缺失)**时,该事件按**原下标**记入 `errors` 且**不生效**;失败事件不影响后续事件。
3. **交接仅作用于无父件实物**:实物当前有直接父件时交接报错。实物的首次成功交接 `from_location` 须为 `null`,此后须等于**当前持有人**。
4. **装入**:子件须无父件、不能与父件相同或为父件祖先(不成环);双方都须**已有持有人**(至少成功交接过一次)且**持有人相同**;装入不改变任何持有人。
5. **取出**:指定父件必须是子件的**当前直接父件**(不可跨级);取出后子件成为无父件实物,**保留当时持有人**(不重复记录地点),此后可自行交接。
6. **容器交接**:`to_location` 同时成为该无父件实物及其包装树内**所有后代**的当前持有人,后代同样按到达顺序记录该地点(同一地点去重,保留首次到达)。
7. 库存实物初始无持有人、地点列表为空;**未命中任何召回规则的实物不出现在响应中**。
8. **同地共处时段**(仅当提供 `contact_until` 时计算):同一 UTC 时刻的事件**全部处理完后**,才以该时刻的状态计算接下来时段的持有人;时段**左闭右开**,最后一段截止 `contact_until`。对每个**正时长**时段,找出**未命中召回**的库存实物与**直接命中**实物持有人为同一地点的共处:**持有人未知**(尚无成功交接)**不计**,**不作传递扩散**(未命中实物不会因与另一未命中实物共处而间接关联到命中实物);同一实物对、地点的**相接时段合并**为一段。

响应:

```json
{
  "items": [
    {
      "gtin": "09506001123478",
      "serial": "ITEM-1",
      "locations": ["WH-A", "WH-B", "WH-C"],
      "rule_ids": ["recall-2026-001"]
    }
  ],
  "errors": [
    { "index": 3, "reason": "装入失败:双方当前持有人须相同,父件持有人为 'WH-A',子件持有人为 'WH-B'" }
  ],
  "contacts": [
    {
      "item": { "gtin": "09506001123461", "serial": "CASE-1" },
      "recalled_item": { "gtin": "09506001123478", "serial": "ITEM-1" },
      "location": "WH-B",
      "start": "2026-09-01T03:00:00Z",
      "end": "2026-09-01T05:00:00Z"
    }
  ]
}
```

- `items` 仅含**命中召回**的库存实物,按 **GTIN、序列号**排序;`locations` 为该实物按**首次到达顺序去重**的保管地点(含随祖先容器交接带入的地点);`rule_ids` 为命中的规则 ID,按规则输入顺序。
- `errors` 按事件**原始下标**排序,`reason` 说明失败原因(扫描非法 / 缺序列号 / 库存中不存在 / 批号 / 有效期冲突 / 有父件仍交接 / 交接链断裂 / 持有人缺失或不一致 / 重复装入 / 成环 / 取出非当前直接父件)。
- `contacts` **仅在提供 `contact_until` 时返回**(无共处时为**空数组** `[]`;省略 `contact_until` 时响应**不含该字段**,与原有一致)。每项为一段未命中实物与直接命中实物的同地共处:`item` 为未命中实物(GTIN+序列号),`recalled_item` 为直接命中实物(GTIN+序列号),`location` 为共处地点,`start` / `end` 为 **UTC** 起止时刻(**左闭右开**)。按未命中实物(GTIN、序列号)、命中实物(GTIN、序列号)、开始时刻、地点排序。
- 请求体不合法(数量超限、规则 ID 重复、`action` 非法、`occurred_at` 缺时区、`contact_until` 缺时区或早于任一事件时间、字段缺失或多余、库存扫描非法 / 缺序列号 / GTIN+序列号重复等)时整个请求返回 **422**。

示例:托盘 `PALLET-1`、箱 `CASE-1` 与命中召回的 `ITEM-1` 先在 `WH-A` 完成各自首次交接,随后装箱、装托盘;托盘整体交接至 `WH-B` 时,箱内实物随之前往;取出后实物保留 `WH-B` 并自行交接至 `WH-C`。

```bash
curl -s http://localhost:8000/v1/recall/location-trace \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": [
      "01095060011234541725123110LOT1\u001d21PALLET-1",
      "01095060011234611725123110LOT1\u001d21CASE-1",
      "01095060011234781725123110LOT1\u001d21ITEM-1"
    ],
    "rules": [{"id": "recall-2026-001", "gtin": "09506001123478", "batch": "LOT1"}],
    "events": [
      {"action": "transfer",
       "scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "occurred_at": "2026-09-01T00:00:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "transfer",
       "scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "occurred_at": "2026-09-01T00:01:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "transfer",
       "scan": "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T00:02:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "pack",
       "parent_scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "child_scan":  "01095060011234611725123110LOT1\u001d21CASE-1",
       "occurred_at": "2026-09-01T01:00:00Z"},
      {"action": "pack",
       "parent_scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "child_scan":  "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T02:00:00Z"},
      {"action": "transfer",
       "scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "occurred_at": "2026-09-01T03:00:00Z", "from_location": "WH-A", "to_location": "WH-B"},
      {"action": "unpack",
       "parent_scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "child_scan":  "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T04:00:00Z"},
      {"action": "transfer",
       "scan": "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T05:00:00Z", "from_location": "WH-B", "to_location": "WH-C"}
    ]
  }'
```

响应中 `ITEM-1` 的 `locations` 为 `["WH-A", "WH-B", "WH-C"]`(`WH-B` 由托盘整体交接带入,取出后保留;`WH-C` 为取出后自行交接)。

**同地共处时段示例**:在上例基础上加入 `contact_until`,即可额外查出未命中召回的库存实物(托盘 `PALLET-1`、箱 `CASE-1`)与直接命中的 `ITEM-1` 在同一保管地点的正时长共处时段:

```bash
curl -s http://localhost:8000/v1/recall/location-trace \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": [
      "01095060011234541725123110LOT1\u001d21PALLET-1",
      "01095060011234611725123110LOT1\u001d21CASE-1",
      "01095060011234781725123110LOT1\u001d21ITEM-1"
    ],
    "rules": [{"id": "recall-2026-001", "gtin": "09506001123478", "batch": "LOT1"}],
    "events": [
      {"action": "transfer",
       "scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "occurred_at": "2026-09-01T00:00:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "transfer",
       "scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "occurred_at": "2026-09-01T00:01:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "transfer",
       "scan": "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T00:02:00Z", "from_location": null, "to_location": "WH-A"},
      {"action": "pack",
       "parent_scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "child_scan":  "01095060011234611725123110LOT1\u001d21CASE-1",
       "occurred_at": "2026-09-01T01:00:00Z"},
      {"action": "pack",
       "parent_scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "child_scan":  "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T02:00:00Z"},
      {"action": "transfer",
       "scan": "01095060011234541725123110LOT1\u001d21PALLET-1",
       "occurred_at": "2026-09-01T03:00:00Z", "from_location": "WH-A", "to_location": "WH-B"},
      {"action": "unpack",
       "parent_scan": "01095060011234611725123110LOT1\u001d21CASE-1",
       "child_scan":  "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T04:00:00Z"},
      {"action": "transfer",
       "scan": "01095060011234781725123110LOT1\u001d21ITEM-1",
       "occurred_at": "2026-09-01T05:00:00Z", "from_location": "WH-B", "to_location": "WH-C"}
    ],
    "contact_until": "2026-09-01T06:00:00Z"
  }'
```

响应在原有 `items` / `errors` 之外附带 `contacts`(此处 `errors` 为空):

```json
{
  "contacts": [
    {
      "item": { "gtin": "09506001123454", "serial": "PALLET-1" },
      "recalled_item": { "gtin": "09506001123478", "serial": "ITEM-1" },
      "location": "WH-A",
      "start": "2026-09-01T00:02:00Z",
      "end": "2026-09-01T03:00:00Z"
    },
    {
      "item": { "gtin": "09506001123454", "serial": "PALLET-1" },
      "recalled_item": { "gtin": "09506001123478", "serial": "ITEM-1" },
      "location": "WH-B",
      "start": "2026-09-01T03:00:00Z",
      "end": "2026-09-01T05:00:00Z"
    },
    {
      "item": { "gtin": "09506001123461", "serial": "CASE-1" },
      "recalled_item": { "gtin": "09506001123478", "serial": "ITEM-1" },
      "location": "WH-A",
      "start": "2026-09-01T00:02:00Z",
      "end": "2026-09-01T03:00:00Z"
    },
    {
      "item": { "gtin": "09506001123461", "serial": "CASE-1" },
      "recalled_item": { "gtin": "09506001123478", "serial": "ITEM-1" },
      "location": "WH-B",
      "start": "2026-09-01T03:00:00Z",
      "end": "2026-09-01T05:00:00Z"
    }
  ]
}
```

`ITEM-1` 于 `00:02` 到达 `WH-A` 后与同处的托盘、箱开始共处;`03:00` 托盘整体交接至 `WH-B` 时三者随动,共处地点变为 `WH-B`;`05:00` `ITEM-1` 离开前往 `WH-C`,共处结束(`contact_until` 为 `06:00`,最后一段 `WH-C` 无未命中实物同处,不产生条目)。

### `POST /v1/receiving/reconcile`

核对到货实扫与期望明细:解析扫描串、对含序列号的实物去重,再把每条有效扫描全局分配到一个兼容期望行的名额上,输出**已收**(各扫描的分配行)、**缺额**(各期望行剩余数量)与**多余扫描**(未分配 / 重复 / 非法)。

请求体:

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `scans` | `string[]` | **1~60** 条 GS1 扫描串,语法同召回核验 |
| `lines` | `object[]` | **1~60** 条期望行,`id` 非空且请求内唯一;全部行 `quantity` 之和 **≤ 60** |

期望行字段:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 非空字符串,请求内唯一 |
| `gtin` | 是 | 14 位数字,须通过 GS1 模 10 校验位 |
| `quantity` | 是 | **正整数**名额数;全部行总数 ≤ 60(布尔值拒绝) |
| `batch` | 否 | 批号,1~20 位可打印 ASCII;附带时扫描串须含 AI 10 且相等 |
| `serial` | 否 | 序列号,1~20 位可打印 ASCII;附带时扫描串须含 AI 21 且相等 |
| `expiry` | 否 | ISO 到期日(`YYYY-MM-DD`),年限 **2000~2099**;附带时扫描串须含 AI 17 且日期相等 |

处理规则:

1. **复用现有 GS1 解析**(含 `]d2` 前缀、FNC1、日 `00` 当月末日等全部语法)。解析失败的扫描按**原下标**在 `errors` 中返回 `offset`(相对原始扫描串,含 `]d2` 前缀)与 `reason`,不参与后续处理。
2. **序列号去重**:含 AI 21 的扫描按 **GTIN + 序列号**全局识别实物,同一实物只计**首次**(按扫描原顺序);后续重复扫描按原下标列入 `duplicate_indices`,不占名额。不含 AI 21 的扫描不做去重。
3. **名额匹配**:每条其余有效扫描最多占用**一个**名额——GTIN 相等,且期望行附带的每个限定字段(批号 / 序列号 / 到期日)在扫描串中**均存在且相等**;扫描多带的字段不影响匹配。
4. **全局分配**(在名额容量约束下依次优化):
   1. **核销数最多**(尽量多的扫描被分配);
   2. 再使**已分配名额的限定字段数总和最大**(优先核销更具体的期望行);
   3. 仍并列时,按**扫描原顺序**取「期望行下标序列」**字典序最小**,未分配视为大于所有行下标。

响应:

```json
{
  "assignments": [
    {"index": 0, "line_id": "line-lot"},
    {"index": 1, "line_id": null}
  ],
  "errors": [
    {"index": 3, "offset": 2, "reason": "AI '01' 长度不足: 需要 14 位数字"}
  ],
  "unassigned_indices": [1, 4],
  "duplicate_indices": [2],
  "lines": [
    {"id": "line-any", "remaining": 1},
    {"id": "line-lot", "remaining": 0}
  ]
}
```

- `assignments` 与 `scans` **等长且同序**;`line_id` 为该扫描核销到的期望行 ID,未分配(非法 / 重复 / 多余)为 `null`。
- `errors`:解析失败扫描,按原始下标排序;非法扫描**不算**多余扫描。
- `unassigned_indices`:未占用任何名额的**有效且非重复**扫描下标(即多余扫描),按原始下标排序。
- `duplicate_indices`:同 GTIN + AI 21 序列号的后续重复扫描下标,按原始下标排序。
- `lines`:各期望行的剩余数量(`quantity − 已核销数`),**保持期望行输入顺序**;剩余即**缺额**,为 0 表示该行已收齐。
- 期望行不合法(GTIN 校验位错误、`id` 为空或重复、`quantity` 非正整数、限定字段非法、到期日越界、数量总数超 60、数组越界、字段缺失或多余等)时整个请求返回 **422**。

示例:期望 3 个泛化 GTIN 名额 + 1 个限定批号 `LOT1` 的名额;4 条扫描中两条 `LOT1`(后者为序列号重复)、1 条别的 GTIN、1 条非法串。

```bash
curl -s http://localhost:8000/v1/receiving/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "scans": [
      "010950600112345410LOT1\x1d21SN1",
      "010950600112345410LOT1\x1d21SN1",
      "010950600112346110LOT2\x1d21SN9",
      "0100"
    ],
    "lines": [
      {"id": "line-any", "gtin": "09506001123454", "quantity": 3},
      {"id": "line-lot", "gtin": "09506001123454", "quantity": 1, "batch": "LOT1"}
    ]
  }'
```

> `09506001123461` 为另一合法 GTIN(校验位不同)。响应中:下标 0 核销到限定行 `line-lot`(同核销数下优先占用限定字段更多的名额);下标 1 为同 GTIN+序列号重复;下标 2 GTIN 不符,是多余扫描;下标 3 解析失败。`line-any` 缺额 3,`line-lot` 收齐(剩余 0)。

## 项目结构

```
app/
  main.py      # FastAPI 入口与路由
  replay.py    # 重放内核:逐事件 transfer/pack/unpack,共享身份/父子边/持有人/成功下标/地点历史
  gs1.py       # GS1 扫描串解析(AI 01/10/17/21、FNC1、]d2、校验位、日期)
  models.py    # 请求 / 响应模型与规则校验
  matcher.py   # 召回规则匹配
  checker.py   # 扫码核验共享逻辑(check / check-signed 复用)
  jws.py       # compact JWS 辅助:无填充 base64url、拒绝重复 JSON 键
  trust.py     # 本地信任文件(kid -> Ed25519 公钥 + 授权 GTIN 前缀)
  revoked.py   # 单包撤销名单(本地 JSON:revoked 摘要数组,每次请求读盘)
  signed.py    # /v1/recall/check-signed:验签 / 时间窗 / 前缀授权 / 单包撤销
  relations.py # 召回规则关系分析(命中集合比较与证据扫描串构造)
  custody.py   # 实物交接审计适配层(扫描/链/批号有效期校验 + 结果整理,内核 custody 模式)
  trim.py      # 交接日志最少剔除(每实物后缀最长链 DP + 全局字典序贪心,锁定必留)
  packing.py   # 装拆包装审计适配层 + replay_packing(内核 packing 模式,隔离计划复用)
  isolation.py # 召回隔离计划(命中实物沿最终父链归并到最高层已封签祖先)
  trace.py     # 召回实物地点追溯适配层(库存基准 + 结果整理 + 未命中实物同地共处时段,内核 trace 模式)
  receiving.py # 到货核对(扫描解析/去重 + 最小费用最大流全局最优核销)
tests/
  test_recall.py
  test_recall_signed.py   # 含单包撤销(名单命中 / 不连带其他包 / 原子替换生效 / 名单只读一次 / 名单非法 500)
  test_relations.py
  test_custody.py
  test_trim.py
  test_packing.py
  test_isolation.py
  test_trace.py
  test_receiving.py
  test_kernel_regression.py   # 统一内核:小规模嵌套、失败后续操作与原子性回归
scripts/
  generate_sample_keys.py            # 生成本地演示密钥、信任文件与请求样例
  verify_revocation_container.sh     # 真实容器(docker compose)覆盖原子替换热更新全过程
trust/                               # 整个目录被 Compose 只读挂载到 /srv/trust(支持原子替换热更新)
  recall-keys.json           # 本地信任文件(不内置进镜像)
  revoked-packages.json      # 单包撤销名单样例(空 revoked 数组)
examples/
  sample-check-signed.json   # check-signed 请求体样例
  sample-private-keys.json   # 样例私钥(仅本地联调;不挂载进容器)
Dockerfile
docker-compose.yml
requirements.txt
```
