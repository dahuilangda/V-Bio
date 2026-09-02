#!/bin/bash
# mmseqs GPU 租约包装器 —— 目标库为 *_gpu 填充库时,先从 celery 共享 GPU 池
# (redis,与 backend/gpu_manager.py 完全同协议: BLPOP available → HSET in_use,
# 心跳 task_heartbeat:<owner>,条件释放)租一块卡,再以 CUDA_VISIBLE_DEVICES 钦定
# + --gpu 1 运行 search。redis 不可用或等待超时则 CPU 降级,绝不阻断 MSA。
# 纯 bash + /dev/tcp,零运行时依赖;由 deploy/docker/DOCKER_CAP_COLABFOLD_SERVER.Dockerfile
# 安装到 /app/mmseqs/bin/mmseqs(Go 后端经 config.json paths.mmseqs 调用)。

mode="${MMSEQS_LOAD_MODE:-}"
if [[ -n "$mode" && ! "$mode" =~ ^[0-9]+$ ]]; then
  mode=""
fi

real_bin="${MMSEQS_REAL_BIN:-/app/mmseqs/bin/mmseqs.real}"
if [[ ! -x "$real_bin" ]]; then
  echo "No usable MMseqs binary found at: ${real_bin}" >&2
  exit 1
fi

cmd="$1"
if [[ -z "$cmd" || "$cmd" == "version" || "$cmd" == "help" || "$cmd" == "list" ]]; then
  exec "$real_bin" "$@"
fi

shift
sanitized_args=()
skip_next=0
for arg in "$@"; do
  if [[ "$skip_next" = 1 ]]; then
    skip_next=0
    continue
  fi
  if [[ "$arg" == "--db-load-mode" ]]; then
    skip_next=1
    continue
  fi
  sanitized_args+=("$arg")
done

# GPU 兼容库(<X>_gpu* 带 .is_gpu_db 标记)的下游参数改写:msa.sh/ColabFold 协议把
# 目标写成 <db>.idx,但 GPU 库的 .idx(subset 10)不含序列查询数据——官方 GPU 协议
# (colabfold_search, sokrypton/ColabFold search.py)在无索引/IGNORE_INDEX 路径用
# <db>_seq 序列库与 <db>_aln 对齐库。这里按官方协议透明改写,target 指向 GPU 库本体:
#   expandaln 的 target→_seq、target2→_aln;其余模块 target→_seq。
# search 不改写(直接扫 padded 库本体),由下方租约分支注入 --gpu 1 --prefilter-mode 1。
rewrite_gpu_target_args() {
  local -n dst=$1; local cmdname=$2; shift 2
  dst=("$@")
  local marker=""
  local a base
  for a in "$@"; do
    base="$a"; [[ "$a" == *.idx ]] && base="${a%.idx}"
    if [[ -e "${base}.is_gpu_db" ]]; then marker="$base"; break; fi
  done
  [[ -z "$marker" ]] && return 0
  local i=0
  local t1=1 t2=3
  case "$cmdname" in
    expandaln) ;;
    align|convertalis|filterresult|result2msa|result2profile) t2=-1 ;;
    *) return 0 ;;
  esac
  for a in "$@"; do
    if [[ $i -eq $t1 && "$a" == *.idx ]]; then
      dst[$i]="${a%.idx}_seq"
    elif [[ $i -eq $t2 && "$a" == *.idx ]]; then
      dst[$i]="${a%.idx}_aln"
    fi
    i=$((i+1))
  done
}

rewrite_gpu_target_args rewritten_args "$cmd" "${sanitized_args[@]}"

case "$cmd" in
  search|expandaln|filterresult|result2msa|result2profile|align|convertalis|prefilter)
    if [[ -n "$mode" ]]; then
      set -- "$cmd" --db-load-mode "$mode" "${rewritten_args[@]}"
    else
      set -- "$cmd" "${rewritten_args[@]}"
    fi
    ;;
  *)
    set -- "$cmd" "${rewritten_args[@]}"
    ;;
esac

# ---- GPU 租约:仅 search + 目标库为 *_gpu 填充库(独立 prefilter 不支持 --gpu;
# 非填充库传 --gpu 会直接报 "not a valid GPU database")-----------------------
want_gpu=0
if [[ "${COLABFOLD_ENABLE_GPU:-0}" == "1" && "$cmd" == "search" ]]; then
  # GPU 库判定:目标参数旁存在 <target>.is_gpu_db 标记文件(官方 GPU 兼容库
  # 部署时 touch 生成;库本身按官方惯例可用标准命名,不依赖后缀约定)。
  for arg in "${sanitized_args[@]}"; do
    if [[ -e "${arg}.is_gpu_db" ]]; then
      want_gpu=1
      break
    fi
  done
fi
if [[ "$want_gpu" != "1" ]]; then
  exec "$real_bin" "$@"
fi

MSA_GPU_REDIS_HOST="${MSA_GPU_REDIS_HOST:-172.17.3.200}"
MSA_GPU_REDIS_PORT="${MSA_GPU_REDIS_PORT:-6379}"
MSA_GPU_WAIT_SECONDS="${MSA_GPU_WAIT_SECONDS:-1800}"
POOL_KEY="${MSA_GPU_POOL_KEY:-boltz_gpu_pool:available}"
IN_USE_KEY="${MSA_GPU_IN_USE_KEY:-boltz_gpu_pool:in_use}"
HEARTBEAT_INTERVAL=20
HEARTBEAT_TTL=60

# 发送一条 RESP 命令,解析单条回复到 REPLY(+status / :int / bulk / array-of-bulk)。
redis_cmd() {
  local req="*$#\r\n" arg
  for arg in "$@"; do
    req+="$"${#arg}"\r\n$arg\r\n"
  done
  exec 3<>"/dev/tcp/$MSA_GPU_REDIS_HOST/$MSA_GPU_REDIS_PORT" || return 1
  printf '%b' "$req" >&3
  REPLY=$(_redis_read_reply)
  local rc=$?
  exec 3>&- 3<&-
  return $rc
}

_redis_read_reply() {
  local line kind len i n out
  IFS= read -r line <&3 || return 1
  line=${line%$'\r'}
  kind=${line:0:1}
  case "$kind" in
    '+'|':') printf '%s' "${line:1}"; return 0 ;;
    '-') return 1 ;;
    '$')
      len=${line:1}
      [[ "$len" == "-1" ]] && return 0
      IFS= read -r -N $((len + 2)) line <&3 || return 1
      printf '%s' "${line:0:len}"
      return 0 ;;
    '*')
      n=${line:1}
      [[ "$n" == "-1" ]] && return 0
      out=()
      for ((i = 0; i < n; i++)); do
        out+=("$(_redis_read_reply)")
      done
      printf '%s\n' "${out[@]}"
      return 0 ;;
    *) return 1 ;;
  esac
}

# 输出 "<gpu> <owner>";rc=1 redis 故障,rc=2 等待超时(两者都 CPU 降级)。
lease_gpu() {
  local owner="colabfold_msa:$$:$(date +%s)"
  local deadline=$(( $(date +%s) + MSA_GPU_WAIT_SECONDS )) slice result value
  while (( $(date +%s) < deadline )); do
    slice=$(( deadline - $(date +%s) ))
    (( slice > 30 )) && slice=30
    (( slice < 1 )) && slice=1
    if ! redis_cmd BLPOP "$POOL_KEY" "$slice"; then
      return 1
    fi
    value=$(printf '%s\n' "$REPLY" | tail -1)
    if [[ -z "$value" ]]; then
      continue
    fi
    if ! redis_cmd HSET "$IN_USE_KEY" "$value" "$owner"; then
      return 1
    fi
    printf '%s %s' "$value" "$owner"
    return 0
  done
  return 2
}

release_gpu() {  # $1=gpu $2=owner;仅当 lease 仍归本任务时释放(多余 RPUSH 由 reconcile 兜底去重)
  local gpu="$1" owner="$2" current
  if redis_cmd HGET "$IN_USE_KEY" "$gpu"; then
    current="$REPLY"
    if [[ "$current" == "$owner" ]]; then
      redis_cmd HDEL "$IN_USE_KEY" "$gpu" >/dev/null
      redis_cmd RPUSH "$POOL_KEY" "$gpu" >/dev/null
    fi
  fi
}

LEASE=$(lease_gpu)
lease_rc=$?
if [[ $lease_rc -ne 0 ]]; then
  if [[ $lease_rc -eq 1 ]]; then
    echo "[msa-gpu] redis pool unreachable; running WITHOUT GPU" >&2
  else
    echo "[msa-gpu] no GPU free within ${MSA_GPU_WAIT_SECONDS}s; CPU fallback" >&2
  fi
  exec "$real_bin" "$@"
fi

GPU_ID=${LEASE%% *}
OWNER=${LEASE#* }
echo "[msa-gpu] leased GPU $GPU_ID ($OWNER)" >&2

(
  while :; do
    redis_cmd SETEX "task_heartbeat:$OWNER" "$HEARTBEAT_TTL" 1 >/dev/null 2>&1
    sleep "$HEARTBEAT_INTERVAL"
  done
) &
HB_PID=$!

CUDA_VISIBLE_DEVICES="$GPU_ID" "$real_bin" "$@" --gpu 1 --prefilter-mode 1
run_rc=$?

kill "$HB_PID" 2>/dev/null
wait "$HB_PID" 2>/dev/null
redis_cmd DEL "task_heartbeat:$OWNER" >/dev/null
release_gpu "$GPU_ID" "$OWNER"
echo "[msa-gpu] released GPU $GPU_ID" >&2
exit $run_rc
