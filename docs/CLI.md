# CLI 상세 사용법

설치와 전체 상태는 [README](../README.md)를 참고하세요. 아래 명령은 `uv tool install .` 후의 `pons` 기준입니다. 소스 루트에서는 `python3 -m pons_cli`로 바꿔 사용할 수 있습니다.

## 공통 옵션

```bash
pons --workspace "$HOME/Videos/pons-work"
pons --data /path/to/private-data --workspace /path/to/workspace
pons status prj_... --data /path/to/private-data --json
```

`--data`, `--workspace`, `--json`은 하위 명령 앞/뒤에 사용할 수 있습니다. 자연어 대화와 setup/attach에는 실제 터미널이 필요합니다. 비대화형 실행에서 입력이나 승인 정보가 빠지면 사용자 입력을 무한 대기하지 않고 exit 3으로 안내합니다.

- data: `--data` → `STUDIO_DATA` → 실행 환경의 기존 `.studio` → XDG data의 `pons-studio`.
- workspace: `--workspace`가 없으면 대화형으로 폴더만 묻습니다.
- 두 디렉터리는 서로 포함할 수 없습니다. 출력은 `workspace/pons-projects/<id>`에, 비밀·DB는 data에 둡니다.
- 구버전 프로젝트의 기존 data/projects/<id>를 이동하지 않습니다. 새 workspace가 삭제되면 원래 경로를 복원해야 합니다. 자동 재연결 UI는 미완성입니다.

## 설정

```bash
pons setup --data /path/to/private-data
pons-skills --source /path/to/authorized/pinned-skills --data /path/to/private-data
```

setup 메뉴는 Codex OAuth, 선택한 시스템 MCP 참조 등록, MCP OAuth, CLI/API 연결 JSON 등록, 외부 `codex-imagen`/`ai-delegation-loop` 경로 연결을 제공합니다. 기존 인증이 확인되면 일반 시작 시 이 전체 메뉴를 요구하지 않습니다.

제작 스킬 원문은 이 저장소/wheel에 포함하지 않습니다. `pons-skills --download`는 **사용자가 명시한 경우에만** upstream에 접근합니다. 지원 커밋은 README에 고정되어 있습니다. 원본 소스나 기존 설치를 덮어쓰지 않으며 모델·공급자를 설치하지 않습니다.

이미 준비한 로컬 instruction 폴더는 `PONS_SKILLS_SOURCE`로 최초 데이터에 가져올 수 있습니다. 정확한 `installed-commit.txt`와 스킬 파일이 있어야 합니다. 원본 Studio의 형제 reference 폴더를 자동 탐색하지 않습니다.

OAuth가 수동 callback을 요구하면 대기 화면의 auth ID로 다른 터미널에서 실행합니다.

```bash
pons auth-reply auth_... --data /path/to/the-same-data
```

URL은 숨김 입력하며 argv/출력에 넣지 않습니다. 자동화는 `--callback-stdin`으로 전달할 수 있습니다. 실제 공급자별 OAuth 호환성은 별도 검증이 필요합니다.

## 대화

```text
pons> 커피 역사로 60초 영상을 만들어줘
이 요청의 실행·공급자 과금 경계를 확인하고 동의 여부 선택
```

현재 동의는 턴별입니다. 거절해도 메시지는 읽기 전용 계획·조회로 전달됩니다.

| 명령 | 설명 |
| --- | --- |
| `/plan <질문>` | 실행 동의 질문 없는 계획·조회 |
| `/run <지시>` | 이번 요청에 동의한 후 제작 지시 |
| `/attach-file <파일>` | 다음 메시지에 자료 첨부 |
| `/projects` | 같은 data의 프로젝트 조회 |
| `/attach <id>` | 정확한 기존 대화/session에 재접속 |
| `/status` | 상태·고정 workflow 단계·영상 견적 |
| `/use speech <연결ID>` | 새 프로젝트의 음성 연결 선택 |
| `/use image <연결ID>` | 새 프로젝트의 이미지 연결 선택 |
| `/video <연결ID>` | 멈춘 프로젝트의 생성형 영상 사용 명시 선택 |
| `/approve-video <견적ID>` | 표시된 정확한 기존 video_runtime 견적 확인·승인 |
| `/stop`, `/resume` | 취소 요청·원격 확인 후 재개 |
| `/settings` | 설정 메뉴 |
| `/exit` | detach. backend 작업은 유지 |

`/use`는 첫 메시지 전에 선택합니다. 연결 발견/등록과 실제 인증/기능 확인은 다릅니다. 없는 기능을 다른 공급자로 자동 대체하지 않습니다.

Ctrl-C는 현재 입력만 취소합니다. 로그인 대기 중에는 그 로그인만 취소합니다. EOF도 작업 삭제가 아닌 detach입니다. 종료 안내에서 같은 `--data`가 들어간 attach 명령을 사용하세요.

## 명령형 제작과 종료 코드

```bash
pons projects list --json
pons status prj_... --json
pons attach prj_...
pons stop prj_... --json
pons resume prj_... --remote-checked --json
```

실제 제작을 비대화형으로 요청하는 아래 예시는 공급자 비용이 발생할 수 있습니다.

```bash
pons make '커피 역사 60초 영상' --workspace /path/to/workspace \
  --execute --allow-provider-charges --json
```

- exit 0: API 요청/조회/접수 성공. 영상 완성의 의미가 아닙니다.
- exit 1: 실행/API 오류.
- exit 2: CLI 인자·입력 오류.
- exit 3: 인증·승인·필수 정보 등 사용자 조치 필요.

실패한 프로젝트를 정상 조회해도 exit 0입니다. JSON의 status와 실제 산출물을 확인하세요.

## 영속 단계와 한계

새 CLI 프로젝트는 고정 단계·시도·lease·산출물 해시를 저장합니다. 선행 단계 미완료, 오래된 lease, 누락/변경 파일은 거절합니다. 진행 메시지 하나로 완료 처리하지 않습니다. `review`와 최종 납품에서는 기존 파일·canonical 검증기를 적용합니다.

`artifact_hashes_only`는 파일/해시 검사 기준이지 의미·창작 품질 검수 완료가 아닙니다. 원격 상태 확인은 현재 사용자 확인 및 기존 TTS/video 중복 방지에 의존합니다. 세 레시피별 완전한 DAG, 프로젝트 범위 예산 gateway, 실행 중 추가 권한 중계, 자동 부분 재제작·일반 원격 대조·export는 남아 있습니다.

원본 GUI·React·Tauri·Node 빌드는 필요하지 않습니다. Python 로컬 backend의 호환 endpoint 명칭에 `desktop`이 남아 있어도 GUI 프로세스를 기동하지 않습니다.
