#!/usr/bin/env bash
#
# 用真实容器覆盖单包撤销名单“宿主机原子替换 -> 容器内立即生效”的完整过程。
#
# 复现/验证内容(全程不重启容器):
#   1. docker compose 单命令构建并启动(非 root、/srv/trust 只读、私钥不在容器内);
#   2. 空名单下,通过验签 / 时间窗 / GTIN 授权的样例包返回 200;
#   3. 宿主机创建 0644(非 root 可读)更新文件并 os.replace 原子替换为“含该包
#      正确摘要”的名单后,下一次请求即 403 且无匹配结果;
#   4. 同一 kid 签发的另一个包仍返回 200(撤销不连带);
#   5. 换回空名单后恢复 200;
#   6. 替换后的名单不可读 / JSON 非法 / 结构或元素非法时返回 500 且无匹配结果,
#      不回退到旧(空)名单(若错误回退,此处会得到 200);
#   7. 全过程容器 ID 不变(证明无需重启),结束后恢复宿主机名单原文件。
#
# 用法(需要 Docker;在仓库根目录执行):
#   scripts/verify_revocation_container.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

REVOKED_HOST="trust/revoked-packages.json"
REQUEST_HOST="examples/sample-check-signed.json"
PRIVATE_HOST="examples/sample-private-keys.json"
BASE_URL="http://localhost:8000"

# 兼容 docker compose v2 与 docker-compose v1
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: 未找到 docker compose / docker-compose" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
trap '
  set +e
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  [ -f "$WORKDIR/revoked.orig" ] && cp "$WORKDIR/revoked.orig" "$REVOKED_HOST"
  chmod 644 "$REVOKED_HOST" 2>/dev/null
  rm -rf "$WORKDIR"
' EXIT

cp "$REVOKED_HOST" "$WORKDIR/revoked.orig"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
yel()   { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { red "FAILED: $*"; exit 1; }

# 宿主机原子写名单:固定权限的临时文件 -> os.replace(rename(2))。
# mode 用 8 进制字符串;默认 644,保证容器内非 root uid(10001)可读。
atomic_write() {
  local content="$1" mode="${2:-644}"
  python3 - "$content" "$mode" "$REVOKED_HOST" <<'PY'
import os, sys
content, mode, path = sys.argv[1], int(sys.argv[2], 8), sys.argv[3]
tmp = os.path.join(os.path.dirname(path), ".revoked-packages.updating.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(content)
os.replace(tmp, path)          # 原子替换;容器下次按路径名打开即见新 inode
os.chmod(path, mode)           # 显式授权,避免 umask 导致非 root 不可读
PY
}

# POST check-signed;$1=请求体文件。设置全局 CODE,BODY 写入 $WORKDIR/body
post_signed() {
  CODE="$(curl -s -o "$WORKDIR/body" -w '%{http_code}' \
    "$BASE_URL/v1/recall/check-signed" \
    -H 'Content-Type: application/json' --data @"$1")"
}
body_has_no_results() {
  python3 - "$WORKDIR/body" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
assert "results" not in b and "kid" not in b, b
PY
}
expect_code() {
  local want="$1" where="$2"
  if [ "$CODE" != "$want" ]; then
    echo "---- response body ----" >&2; cat "$WORKDIR/body" >&2; echo >&2
    die "$where: 期望 HTTP $want,实际 HTTP $CODE"
  fi
}

# 1) 单命令构建并启动
grn "==> docker compose up --build -d"
"${COMPOSE[@]}" up --build -d

grn "==> 等待 /healthz 就绪"
for _ in $(seq 1 60); do
  if curl -sf "$BASE_URL/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "${ready:-0}" = 1 ] || die "服务在 60s 内未就绪"

# 运行身份与只读挂载/私钥隔离的事实核对
UID_IN_CONTAINER="$("${COMPOSE[@]}" exec -T api id -u | tr -d '\r')"
[ "$UID_IN_CONTAINER" = "10001" ] || die "容器应以非 root uid 10001 运行,实际 $UID_IN_CONTAINER"
if "${COMPOSE[@]}" exec -T api sh -c 'echo x > /srv/trust/.write-probe' 2>/dev/null; then
  die "/srv/trust 应为只读挂载,容器内竟然可写"
fi
if "${COMPOSE[@]}" exec -T api sh -c 'test -e /srv/trust/sample-private-keys.json'; then
  die "样例私钥不得出现在容器挂载目录 /srv/trust 内"
fi
grn "    容器 uid=$UID_IN_CONTAINER(非 root);/srv/trust 只读;私钥未挂载进容器"

CID_BEFORE="$("${COMPOSE[@]}" ps -q api | tr -d '\r')"

# 从实际样例请求计算包标识 SHA-256(header.payload);同时取 kid/now 构造同 kid 兄弟包
digest_line="$(python3 - "$REQUEST_HOST" <<'PY'
import base64, hashlib, json, sys
doc = json.load(open(sys.argv[1]))
h, p, _sig = doc["jws"].split(".")
digest = hashlib.sha256(f"{h}.{p}".encode("ascii")).hexdigest()
header = json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4)))
print(digest, header["kid"], doc["now"])
PY
)"
# shellcheck disable=SC2086
set -- $digest_line
DIGEST="$1"; KID="$2"; NOW="$3"
grn "==> 样例包标识(kid=$KID, now=$NOW):$DIGEST"

# 在容器内用样例私钥(经环境变量传入,不挂载进容器)签发同 kid 的另一个包
grn "==> 在容器内构造同 kid 的另一个已签名包(兄弟包)"
PRIV="$(cat "$PRIVATE_HOST")"
"${COMPOSE[@]}" exec -T -e PRIV="$PRIV" -e KID="$KID" -e NOW="$NOW" api \
  python3 -c '
import base64, json, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
priv = json.loads(os.environ["PRIV"])["keys"][0]["private_key"]
raw = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))
pk = Ed25519PrivateKey.from_private_bytes(raw)
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
FNC1 = "\x1d"
gtin2 = "09506001123461"  # 同授权前缀 0950600,校验位合法
scan2 = "]d2" + "01" + gtin2 + "17" + "251231" + "10" + "LOT1" + FNC1 + "21SN9"
rule = {"id": "recall-2026-002", "gtin": gtin2, "batch": "LOT1",
        "date_from": "2025-01-01", "date_to": "2026-12-31"}
now = int(os.environ["NOW"])
header = {"alg": "EdDSA", "kid": os.environ["KID"]}
payload = {"nbf": now - 300, "exp": now + 86400, "rules": [rule]}
h = b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
p = b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
sig = b64(pk.sign(f"{h}.{p}".encode("ascii")))
json.dump({"scans": [scan2], "jws": f"{h}.{p}.{sig}", "now": now},
          open("/tmp/req2.json", "w"))
'
"${COMPOSE[@]}" exec -T api cat /tmp/req2.json > "$WORKDIR/req2.json"

echo
yel "[1] 空名单:样例包与兄弟包都应 200"
atomic_write '{"revoked": []}' 644
post_signed "$REQUEST_HOST"; expect_code 200 "空名单样例包"
grn "    样例包 HTTP 200"
post_signed "$WORKDIR/req2.json"; expect_code 200 "空名单兄弟包"
python3 - "$WORKDIR/body" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
assert b["results"][0]["matches"] == ["recall-2026-002"], b
PY
grn "    同 kid 兄弟包 HTTP 200,matches=['recall-2026-002']"

echo
yel "[2] 宿主机创建非 root 可读更新文件并 os.replace 为含该包正确摘要的名单"
atomic_write "{\"revoked\": [\"$DIGEST\"]}" 644
stat -c '    宿主机新名单权限: %a(非 root 可读)' "$REVOKED_HOST"
post_signed "$REQUEST_HOST"; expect_code 403 "命中名单的样例包"
body_has_no_results
grn "    下一次请求即 HTTP 403,且无 results/kid(无需重启)"

echo
yel "[3] 同 kid 的其他包仍可核验(撤销不连带)"
post_signed "$WORKDIR/req2.json"; expect_code 200 "同 kid 兄弟包不应被连带撤销"
python3 - "$WORKDIR/body" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
assert b["kid"] and b["results"][0]["matches"] == ["recall-2026-002"], b
PY
grn "    兄弟包仍 HTTP 200,matches=['recall-2026-002']"

echo
yel "[4] 原子换回空名单,样例包恢复 200"
atomic_write '{"revoked": []}' 644
post_signed "$REQUEST_HOST"; expect_code 200 "换回空名单的样例包"
grn "    样例包恢复 HTTP 200"

echo
yel "[5] 非法替换一律 500 且无匹配结果,不回退旧(空)名单(回退会错误地得到 200)"
run_invalid_case() {
  local desc="$1" content="$2" mode="${3:-644}"
  atomic_write "$content" "$mode"
  post_signed "$REQUEST_HOST"
  expect_code 500 "$desc"
  body_has_no_results
  grn "    $desc -> HTTP 500,无 results/kid"
}
run_invalid_case "名单不可读(权限 000)"            '{"revoked": []}' 000
run_invalid_case "JSON 非法(not json)"             'not json'
run_invalid_case "结构非法(顶层是数组)"            '[]'
run_invalid_case "结构非法(缺 revoked)"            '{}'
run_invalid_case "结构非法(额外键)"                '{"revoked": [], "extra": 1}'
run_invalid_case "结构非法(revoked 不是数组)"      '{"revoked": "x"}'
run_invalid_case "元素非法(整数)"                  '{"revoked": [1]}'
run_invalid_case "元素非法(非十六进制字符串)"      '{"revoked": ["ABC"]}'
run_invalid_case "元素非法(大写摘要)" \
  '{"revoked": ["9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08"]}'
run_invalid_case "元素非法(长度 63)" \
  '{"revoked": ["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a0"]}'

echo
yel "[6] 恢复合法空名单后立即恢复 200"
atomic_write '{"revoked": []}' 644
post_signed "$REQUEST_HOST"; expect_code 200 "恢复后的样例包"
grn "    恢复 HTTP 200"

echo
CID_AFTER="$("${COMPOSE[@]}" ps -q api | tr -d '\r')"
[ "$CID_BEFORE" = "$CID_AFTER" ] || die "容器 ID 发生变化,说明中途被重启"
grn "==> 全程容器 ID 未变($CID_AFTER),证明热更新无需重启"

echo
grn "ALL CHECKS PASSED ✅"
