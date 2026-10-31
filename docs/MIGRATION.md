# 서버 이관

코드는 GitHub에서 받고, 데이터는 **구 서버 → 신 서버로 직접 복사**합니다. 데이터는 작습니다(DB 약 15 MB, 고객정보 원본, 메일 접속 정보 수백 KB). 중단 시간은 10분 정도이고, 중단 중 도착한 메일은 메일 서버에 그대로 있으므로 신 서버의 첫 수집에서 읽힙니다.

## 무엇이 어떻게 옮겨지나

| 항목 | 구 서버 경로 | 옮기는 방법 |
|---|---|---|
| 코드 | `/data/csp-notice/app` | `git clone` (GitHub `OWNER/notice`) |
| DB: 수집 이력(UIDL 상태 포함), 고객정보, 안내문, 확인 이력, 사내 계정, 별도 발송 설정, 로그인 계정, 건의사항 | `/data/csp-notice/var/db.sqlite3` | 번들 (`ops/migrate/export.sh`) |
| 메일 접속 정보, 추가 메일함, 웹 비밀키 | `/etc/csp-notice/` | 번들 |
| 고객정보 CSV, 엑셀 원본 | `/data/csp-notice/private/` | 번들 |
| 매일 밤 백업 | `/data/csp-notice/backup/` | 옮기지 않음 (신 서버가 그날 밤부터 새로 만듦) |
| 정적 파일, 운영 코드 사본 | `/data/csp-notice/{static,release}` | 옮기지 않음 (배포가 다시 만듦) |

**지켜야 할 것**

- 번들에는 고객정보와 메일 비밀번호가 들어 있습니다. 서버에서 서버로 `scp`로만 옮기고, git, 메일, 개인 PC를 거치지 않습니다. 확인이 끝나면 양쪽에서 지웁니다.
- 수집기는 **한 번에 한 서버에서만** 돕니다. 어느 메일까지 읽었는지(UIDL)는 DB에 있으므로 신 서버는 구 서버가 멈춘 지점부터 이어서 읽습니다. 두 서버가 함께 돌면 원본이 지워지지는 않지만(수집기는 삭제하지 않음) 같은 메일을 두 번 읽고 메일 서버에 부담을 줍니다. `export.sh`가 구 서버의 유닛을 **disable**까지 하는 이유입니다(재부팅해도 다시 뜨지 않게).
- 구 서버는 `ops/deploy.sh`로 **GitHub에 있는 커밋**이 배포된 상태여야 합니다. `import.sh`는 번들의 `revision`이 신 서버의 저장소에 있는지 확인하고, 없으면 멈춥니다(코드가 DB 스키마보다 오래되면 안 되므로).

## 0. 신 서버 준비 (언제든)

Rocky/RHEL 8 또는 9, 사내망 IP, `mail.example.com:110`에 닿아야 합니다.

```bash
dnf -y install git
git clone https://github.com/OWNER/notice.git /data/csp-notice/app   # 읽기만 하면 https로 충분
/data/csp-notice/app/ops/migrate/bootstrap.sh
```

`bootstrap.sh`는 Python 3.12, nginx 설치, `cspnotice` 계정, 디렉터리, venv를 만들고 시간대(Asia/Seoul)와 메일 서버 도달 여부를 알려 줍니다. 구 서버와 달리 SELinux, firewalld가 켜져 있으면 필요한 설정(httpd_can_network_connect, 80 포트)도 해 줍니다.

## 1. 예행연습 (권장, 구 서버는 계속 운영)

```bash
# 구 서버
/data/csp-notice/app/ops/migrate/export.sh --rehearsal
scp /data/csp-notice/migrate/csp-notice-*-rehearsal.tgz* root@<신 서버>:/data/csp-notice/migrate/

# 신 서버
/data/csp-notice/app/ops/migrate/import.sh /data/csp-notice/migrate/csp-notice-<날짜>-rehearsal.tgz --rehearsal
```

신 서버에는 웹만 뜹니다(수집, 백업 없음). `http://<신 서버 IP>/`에 기존 아이디로 로그인해 대시보드, 작업 목록, 협력기관 탭, 고객 정보가 구 서버와 같은지 봅니다. 번들의 `MANIFEST`에 적힌 건수와 `import.sh` 출력이 같아야 합니다. 끝나면 번들을 양쪽에서 지웁니다.

자동 발송을 쓰는 중이면 예행연습 서버의 `/etc/csp-notice/sens.env`는 `SEND_MODE=off`로 바꿔 두세요(번들에 함께 옮겨지므로). 예행연습 서버에서는 수집이 돌지 않아 자동 발송은 없지만, 화면의 테스트, 발송 버튼은 동작합니다.

## 2. 이관 당일

1. 팀에 알립니다(10분 정도 사이트가 멈추고, 주소가 바뀜).
2. 구 서버에서 최신 코드가 배포되어 있는지 확인합니다: `cat /data/csp-notice/release/REVISION`이 `git -C /data/csp-notice/app log -1 --format=%H`와 같고, 그 커밋이 push되어 있어야 합니다.
3. 구 서버: 서비스를 멈추고 번들을 만듭니다.
   ```bash
   /data/csp-notice/app/ops/migrate/export.sh
   scp /data/csp-notice/migrate/csp-notice-<날짜>.tgz* root@<신 서버>:/data/csp-notice/migrate/
   ```
4. 신 서버: 코드를 최신으로 하고 번들을 넣습니다. `import.sh`가 배포(`ops/deploy.sh`)와 유닛 활성화까지 합니다.
   ```bash
   git -C /data/csp-notice/app pull
   /data/csp-notice/app/ops/migrate/import.sh /data/csp-notice/migrate/csp-notice-<날짜>.tgz
   ```
5. 확인합니다.
   - `journalctl -u csp-notice-scan -n 20 --no-pager`: 5분 안에 수집이 한 번 돌고 `login_failed` 없이 끝나는지.
   - 웹: 로그인, 대시보드의 수집 상태, 확인 필요 건수가 구 서버와 같은지. 로그인 세션과 비밀번호는 그대로입니다(비밀키, DB를 같이 옮겼으므로).
   - `curl -sI http://<신 서버 IP>/ | head -1` → `302`(로그인으로).
6. 팀에 새 주소를 알립니다. DNS 이름을 쓰면 `/etc/csp-notice/site.env`의 `CSP_ALLOWED_HOSTS`에 추가하고 `systemctl restart csp-notice-web`.

## 3. 이관 후

- 구 서버는 **멈춘 채로** 1~2주 둡니다. 되돌려야 하면 구 서버에서 `systemctl enable --now csp-notice-scan.timer csp-notice-scan.path csp-notice-backup.timer csp-notice-web.service`로 다시 올리되, 그 사이 신 서버가 모은 데이터는 구 서버에 없으므로 **신 서버를 먼저 `export.sh`로 멈추고 반대 방향으로 `import.sh`** 합니다.
- 번들(`/data/csp-notice/migrate/`)을 양쪽에서 지웁니다.
- 구 서버를 폐기하기 전에 `/etc/csp-notice`와 `/data/csp-notice/{var,backup,private}`를 지웁니다.

## 되돌리기 (import가 중간에 실패했을 때)

- 번들 검증(sha256, MANIFEST)에서 멈추면 신 서버는 아무것도 바뀌지 않습니다. 다시 `scp`하면 됩니다.
- 그 뒤에 멈추면 신 서버의 이전 DB는 `/data/csp-notice/var/db.sqlite3.before-<시각>`으로 남아 있습니다. 실패 원인을 고치고 같은 번들로 `import.sh`를 다시 실행하면 됩니다(번들은 그대로이고 구 서버는 멈춰 있으므로 안전).
- 구 서버는 `export.sh`가 끝난 뒤에도 데이터가 그대로이며 위 명령으로 다시 올릴 수 있습니다.
