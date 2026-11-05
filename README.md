# Pons Studio CLI

**터미널에서 자연어로 영상 제작을 맡기는 로컬 제작 에이전트.**

Pons Studio의 Python 제작 엔진을 별도 프로젝트로 분리했습니다. React·Tauri·Node 없이 실행하며 Codex CLI와 선택한 MCP·외부 CLI를 연결합니다.

> **개발 중 · `0.3.0.dev0`**
> 대화·기존 인증 재사용·제작 요청·진행 확인·영속 단계 관리가 구현돼 있습니다. 모든 영상 레시피의 자동 완성이나 실계정 제작 성공을 보장하는 완성 제품은 아닙니다. [현재 제한](#현재-제한)을 먼저 확인하세요.

## 어떤 경험인가요?

```text
$ pons --workspace ~/Videos/pons-work

기존 인증을 재사용합니다. 설정을 건너뜁니다.
pons> 커피 역사로 60초 영상을 만들어줘

실행 범위·공급자 과금 안내
이번 요청의 실행에 동의하나요? [y/N] y

[기존 제작 엔진이 요청을 접수하고 실제 작업·차단 상태를 표시]
```

이미 Codex에 로그인했다면 작업 폴더만 지정합니다. 필요한 도구가 없으면 설정 메뉴에서 해당 연결을 준비합니다. 브라우저 인증 동의는 사용자가 수행하며 토큰을 추출하거나 인증을 우회하지 않습니다.

## 준비 사항

- Python **3.11+** — 현재 실행 검증은 Linux/Python 3.14에서 수행합니다.
- Codex CLI — 실제 AI 대화·제작에 필요합니다.
- FFmpeg·ffprobe — 실제 음성 측정·영상 조립에 필요합니다.
- 고정 버전 제작 스킬과 요청한 경로의 외부 실행기.
- 사용할 공급자의 MCP/CLI/API 인증과 해당 사용 권한.

도구·계정·스킬 이름만 등록했다고 기능이 설치되는 것은 아닙니다. macOS는 아직 검증하지 않았으며 Windows는 지원하지 않습니다.

## 설치와 빠른 시작

### 독립 CLI 설치

[uv](https://docs.astral.sh/uv/)가 준비된 환경에서, 접근 권한이 있는 이 저장소를 내려받습니다.

```bash
git clone https://github.com/DeclanJeon/pons-studio-cli.git
cd pons-studio-cli
uv tool install .
pons doctor
```

위 명령은 사용자가 선택해서 실행하는 설치 절차입니다. CLI를 실행하는 것만으로 소프트웨어를 자동 설치하지 않습니다. 비공개 저장소는 GitHub 인증·접근 권한이 필요합니다.

### 기존 데이터 재사용

이미 Pons Studio에서 준비한 개인 데이터가 있다면 그대로 지정할 수 있습니다. 원본 프로젝트/데이터를 자동 이동·병합하지 않습니다.

```bash
pons --data /path/to/existing/private-data \
  --workspace "$HOME/Videos/pons-work"
```

같은 데이터의 업데이트된 backend가 실행 중이면 단일 worker를 공유합니다. 구버전 backend가 CLI 프로토콜을 지원하지 않으면 기존 제작을 마친 뒤 종료하고 새 backend로 다시 실행하세요.

### 새 데이터에서 제작 스킬 준비

제작 스킬 원문과 제3자 실행기는 이 저장소/배포 파일에 포함하지 않습니다. 권한 있는 기존 설치 폴더를 복사하거나, **명시적으로** 고정 upstream을 가져옵니다.

```bash
# 선택 1: 이미 준비된 고정 버전 소스/설치 폴더
pons-skills --source /path/to/authorized/skills \
  --data "$HOME/.local/share/pons-studio"

# 선택 2: 원본 저장소에 접근할 권한이 있을 때만 명시적 다운로드
pons-skills --download --data "$HOME/.local/share/pons-studio"
```

지원 커밋: `968b571161d44f52fbd91cf8f536096fc0e997c7`. 원본 체크아웃·기존 스킬은 덮어쓰지 않습니다. 이 단계가 모델·공급자 런타임을 설치하거나 새 사용 권한을 부여하지는 않습니다.

다른 개인 데이터에 설치했다면 이후 `pons --data <같은 경로>`를 사용하세요. 준비된 폴더를 최초 실행 때 읽게 하려면 `PONS_SKILLS_SOURCE=/path/to/prepared-skills`도 사용할 수 있습니다. 해당 폴더에는 일치하는 `installed-commit.txt`와 실제 스킬 파일이 필요합니다.

### 대화 시작

```bash
pons --workspace "$HOME/Videos/pons-work"
```

인증이 없으면 필요한 로그인 단계만 안내합니다. 외부 `codex-imagen`, `ai-delegation-loop` 원본 경로와 공급자는 `pons setup`에서 연결합니다. 미연결 필수 기능은 정확한 blocker로 표시하며 다른 공급자로 몰래 전환하지 않습니다.

## 작업 폴더와 개인 데이터

| 위치 | 내용 |
| --- | --- |
| `--workspace` | 자료·산출물. 새 프로젝트는 `pons-projects/<id>/` 아래 생성 |
| `--data` | 개인 SQLite·vault·인증 설정 참조·실행 endpoint |
| 기존 프로젝트 | 기존 `<data>/projects/<id>` 위치 유지 |

선택한 두 폴더는 서로 포함할 수 없습니다. 기본 data는 `STUDIO_DATA`, 기존 로컬 `.studio`, XDG data 순으로 선택합니다. 전체 홈 폴더 대신 전용 작업 폴더를 사용하세요. 인증정보·DB를 Git에 넣지 마세요.

## 대화 명령

| 명령 | 역할 |
| --- | --- |
| 자연어 입력 | 실행 동의 확인 후 전달. 거절하면 계획·조회 모드 |
| `/plan <질문>` | 생성 실행 없이 대화·계획 |
| `/run <제작 지시>` | 이번 요청의 실행 동의 후 제작 |
| `/attach-file <파일>` | 다음 메시지에 자료 첨부 |
| `/projects`, `/attach <id>` | 제작 목록·기존 대화 연결 |
| `/status` | 상태·단계·영상 견적 확인 |
| `/stop`, `/resume` | 중단·원격 상태 확인 후 재개 |
| `/settings` | 로그인·MCP·CLI/API·외부 스킬 설정 |
| `/exit` | 터미널 연결 종료. worker는 유지 |

Ctrl-C는 입력을 취소하며, 로그인 대기 중에는 그 로그인만 취소합니다. EOF도 작업 삭제가 아닌 detach입니다.

```bash
pons projects list --json
pons status prj_... --json
pons attach prj_...
pons resume prj_... --remote-checked --json
```

실제 제작을 비대화형으로 요청하려면 별도의 실행/과금 플래그가 필요합니다. **아래 명령은 실제 생성과 공급자 비용이 발생할 수 있습니다.**

```bash
pons make '커피 역사 60초 영상' \
  --workspace /path/to/workspace \
  --execute --allow-provider-charges --json
```

`queued`와 exit 0은 접수 성공이지 납품 완료가 아닙니다. 결과의 `status`와 실제 파일·검수 근거를 확인하세요. 상세 명령은 [docs/CLI.md](docs/CLI.md)에 있습니다.

## 비용·보안 경계

- 현재 실행 동의는 **턴별**입니다. 자동 게시 승인은 포함하지 않습니다.
- MCP/CLI 연결이 무료라는 뜻은 아니며, 금액 상한을 보장하지 않습니다.
- 생성형 동영상은 기본값이 아닙니다. 명시 선택과 기존 정확한 견적 승인을 따릅니다.
- 불확실한 제출을 자동 반복하지 않는 기존 TTS/video 장치를 유지합니다. 일반 공급자 전체의 자동 대조는 아직 미완성입니다.
- 로컬 단일 운영자 도구입니다. 동일 OS 사용자·에이전트에 대한 강한 보안 격리나 공개 SaaS 격리를 제공하지 않습니다.
- 실제 OAuth·유료 공급자 통합과 창작 품질 검증은 별도 동의·환경에서 수행해야 합니다.

## 현재 제한

아직 구현/검증이 남은 기능:

- 프로젝트 범위의 세밀한 승인·철회/만료·강제 예산 gateway.
- 실제 app-server schema 기반 실행 중 추가 권한/elicitation 중계.
- 레시피별 완전한 DAG와 촬영본/생성형 동영상 timeline v2.
- 자동 수정 영향 분석·부분 재제작·일반 원격 작업 대조·안전 export.
- macOS 지원 검증과 스크린샷 기반 터미널 시각 QA.

기존 정지 이미지 조립 경로와 연결된 영상 gateway가 있지만, 모든 생성형 영상을 자동 제작·납품하는 end-to-end 성공을 주장하지 않습니다.

## 개발과 테스트

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m pons_cli --help
uv run python -m compileall -q pons_cli server
```

테스트는 실제 SQLite·loopback HTTP·PTY·FFmpeg와 fake Codex를 사용합니다. 기본 테스트에서 OAuth 로그인·유료 생성은 실행하지 않습니다.

외부 제작 스킬의 canonical validator 통합 시험은 소스가 없으면 명시적으로 skip합니다. 권한 있는 고정 버전 소스가 있을 때만 다음처럼 추가 검증합니다.

```bash
PONS_TEST_SKILLS_SOURCE=/path/to/authorized/pinned-source \
  uv run python -m unittest discover -s tests -v
```

CI는 수동 실행 workflow만 제공하며 push만으로 유료/장시간 작업을 자동 실행하지 않습니다. 로컬 분리·패키징 검증 결과는 [docs/VALIDATION.md](docs/VALIDATION.md)에 기록합니다.

## 프로젝트 구조

```text
pons_cli/          대화형 셸·설정·HTTP 클라이언트
server/            로컬 제작 엔진·큐·저장소·공급자 연결
server/resources/  배포에 포함되는 제작 인계·영상 adapter 계약
tests/             Python 회귀·HTTP·PTY·분리 검증
scripts/           소스 체크아웃용 보조 명령
docs/              명령 안내·분리 과정·검증 결과
```

이 저장소는 원본 Studio 체크아웃, React, Tauri 또는 Node 빌드에 의존하지 않습니다. 외부 제작 스킬과 공급자 계정은 별도 의존성입니다.

## 라이선스·출처

현재 별도의 오픈소스 라이선스를 부여하지 않았습니다. 외부 스킬·공급자·의존성에는 각각의 조건이 적용됩니다. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하세요.
