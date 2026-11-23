#!/bin/bash
# battery-db API 설치·업데이트 (VM에서 root로). 여러 번 실행해도 된다.
#   sudo bash deploy/install.sh <api 폴더>      예: sudo bash /tmp/api/deploy/install.sh /tmp/api
#
# 하는 일
#   1. 서비스 계정 battery-api (로그인 불가)
#   2. /opt/battery-api 에 코드 + venv
#   3. /etc/battery-api/keys.json (API 키 해시), 키 관리 명령 battery-api-key
#   4. DB 접속: 비밀번호 없이 유닉스 소켓 peer 인증. OS 계정 battery-api 만
#      DB 역할 reader / writer_2a / writer_pack 으로 들어갈 수 있게 pg_ident 매핑
#   5. systemd 서비스 battery-api (127.0.0.1:8000)
set -euo pipefail
SRC=$(realpath "${1:?usage: install.sh <api dir>}")
DEST=/opt/battery-api
PGDIR=/etc/postgresql/16/main

dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q python3-venv; }
id battery-api >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin battery-api

# 코드
install -d -m 755 "$DEST"
rm -rf "$DEST/app.new"
cp -r "$SRC/app" "$DEST/app.new"
find "$DEST/app.new" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$DEST/app" && mv "$DEST/app.new" "$DEST/app"
cp "$SRC/requirements.txt" "$DEST/"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"
chown -R root:root "$DEST"

# 키 파일 + 관리 명령
install -d -o root -g battery-api -m 750 /etc/battery-api
if [ ! -f /etc/battery-api/keys.json ]; then
  echo '{"keys": []}' > /etc/battery-api/keys.json
  chown root:battery-api /etc/battery-api/keys.json && chmod 640 /etc/battery-api/keys.json
fi
cat > /usr/local/bin/battery-api-key <<'EOF'
#!/bin/sh
# API 키 관리: battery-api-key add <이름> [--scope read|write:2a|write:pack]... / list / revoke <이름>
cd /opt/battery-api && exec /opt/battery-api/venv/bin/python -m app.keys "$@"
EOF
chmod 755 /usr/local/bin/battery-api-key

# DB peer 매핑 ("local all all peer" 줄보다 위에 있어야 먼저 걸림)
HBA_LINE='local   battery         reader,writer_2a,writer_pack            peer map=batteryapi'
if ! grep -q 'map=batteryapi' "$PGDIR/pg_hba.conf"; then
  cp "$PGDIR/pg_hba.conf" "$PGDIR/pg_hba.conf.bak-$(date +%F)"
  awk -v l="$HBA_LINE" '!d && /^local[ \t]+all[ \t]+all[ \t]+peer/ {print "# battery-api 서비스 (api/deploy/install.sh)"; print l; d=1} {print}' \
      "$PGDIR/pg_hba.conf" > "$PGDIR/pg_hba.conf.new"
  install -o postgres -g postgres -m 640 "$PGDIR/pg_hba.conf.new" "$PGDIR/pg_hba.conf" && rm "$PGDIR/pg_hba.conf.new"
fi
for r in reader writer_2a writer_pack; do
  grep -Eq "^batteryapi[[:space:]]+battery-api[[:space:]]+$r\$" "$PGDIR/pg_ident.conf" \
    || echo "batteryapi      battery-api             $r" >> "$PGDIR/pg_ident.conf"
done
systemctl reload postgresql

# 서비스
install -m 644 "$SRC/deploy/battery-api.service" /etc/systemd/system/battery-api.service
systemctl daemon-reload
systemctl enable -q battery-api
systemctl restart battery-api
for i in $(seq 20); do curl -fs http://127.0.0.1:8000/health >/dev/null && break; sleep 1; done
systemctl is-active battery-api
curl -fsS http://127.0.0.1:8000/health; echo
