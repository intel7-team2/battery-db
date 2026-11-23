#!/bin/bash
# 설치 위치: /usr/local/bin/sync-gh-keys.sh (api/deploy/install.sh)
# GitHub 공개 키로 authorized_keys 두 개를 다시 만든다 (gh-keys.timer, 5분마다).
#   관리자: /etc/gh-key-users    → ubuntu  (쉘·sudo)       + 고정 키 /home/ubuntu/.ssh/authorized_keys.static
#   팀원:   /etc/gh-tunnel-users → tunnel  (DB 포트 포워딩만, sshd_config.d/70-tunnel.conf)
# GitHub 조회가 하나라도 실패하면 그 계정 파일은 그대로 둔다.
set -uo pipefail

build() {  # build <user list> <prefix for each key> <static file or ""> <out tmp>
  local list=$1 prefix=$2 static=$3 tmp=$4 u keys
  [ -n "$static" ] && cat "$static" > "$tmp"
  [ -f "$list" ] || return 0
  while read -r u; do
    u=${u%%#*}; u=${u//[[:space:]]/}
    [ -z "$u" ] && continue
    keys=$(curl -fsS --max-time 15 "https://github.com/$u.keys") || { logger -t gh-keys "fetch $u failed; keeping current file"; return 1; }
    echo "$keys" | grep -E '^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)' | sed "s|^|$prefix|; s|\$| gh:$u|" >> "$tmp"
  done < "$list"
}

install_if_changed() {  # <tmp> <out> <owner>
  if ! cmp -s "$1" "$2"; then
    install -o "$3" -g "$3" -m 600 "$1" "$2"
    logger -t gh-keys "$2 updated: $(grep -c . "$2") keys"
  fi
}

T1=$(mktemp); T2=$(mktemp); : > "$T2"
if build /etc/gh-key-users "" /home/ubuntu/.ssh/authorized_keys.static "$T1"; then
  install_if_changed "$T1" /home/ubuntu/.ssh/authorized_keys ubuntu
fi
if build /etc/gh-tunnel-users 'restrict,port-forwarding,permitopen="localhost:5432",permitopen="127.0.0.1:5432",permitopen="localhost:8000",permitopen="127.0.0.1:8000" ' "" "$T2"; then
  install_if_changed "$T2" /home/tunnel/.ssh/authorized_keys tunnel
fi
rm -f "$T1" "$T2"

# 목록에서 빠진 팀원의 API 키(self 발급)도 폐기
[ -x /usr/local/bin/battery-api-key ] && /usr/local/bin/battery-api-key prune-gh | logger -t gh-keys
exit 0
