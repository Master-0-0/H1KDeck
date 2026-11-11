#!/usr/bin/env bash
# 清理隔离三层内网实验环境。
#   sudo bash lab/teardown_lab.sh
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$LAB_DIR/run"
BR=hlab-br0
HOPS=(hlab-hop1 hlab-hop2 hlab-hop3)
TARGETS=(hlab-t1 hlab-t2 hlab-t3)

[[ ${EUID} -eq 0 ]] || { echo "需要 root：sudo bash $0" >&2; exit 1; }

for ns in "${TARGETS[@]}" "${HOPS[@]}"; do
  if ip netns list | awk '{print $1}' | grep -qx "$ns"; then
    echo "删除 netns $ns"
    ip netns del "$ns"
  fi
done

if ip link show "$BR" >/dev/null 2>&1; then
  echo "删除网桥 $BR"
  ip link del "$BR"
fi

for r in 10.10.10.0/24 10.10.20.0/24 10.10.30.0/24; do
  ip route del "$r" 2>/dev/null && echo "删除路由 $r" || true
done

rm -f "$RUN_DIR"/execd-*.pid
echo "完成。可用 'ip netns list' / 'ip route' 确认残留。"
