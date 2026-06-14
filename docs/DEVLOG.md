# 개발자 일지 (DEVLOG)

RTSP Live Viewer의 개발 과정과 주요 결정을 기록한다. 최신 항목이 위로 온다.

---

## 배경 (출발점)

이 프로젝트는 SigmaStar/MStar Infinity SoC 기반 디지털 사이니지 플레이어(펌웨어 1.8.2)
분석에서 출발했다. 장비의 메인 앱 `box.d4_uni`는 ARM 바이너리 + `libmi_*`(SoC SDK)에
묶여 있어 하드웨어 없이는 구동 불가했고, 장비가 받던 RTSP 소스(`rtsp://10.10.3.50/0`,
H.265 1080p)를 **하드웨어 없이 브라우저로 보는** 것이 핵심 요구로 정리되었다. 그 결과
"재생 전용" 독립 도구로 분리한 것이 본 프로젝트다.

---

## 2026-06-14 — 보안 하드닝 (커밋 리뷰 대응)

- 자동 보안 리뷰가 2건 지적:
  - [HIGH] `/hls/` 인증 우회 — 인증을 켜도 영상 세그먼트가 무인증 접근. → **면제 제거**(브라우저가
    realm 자격증명을 재사용하므로 hls.js도 자동 인증). 인증 켜면 `/hls/`·정적·API 모두 보호 확인.
  - [MEDIUM] 고정 nonce("tick") 리플레이 — 장비 디제스트 모방 잔재. → **프로세스 시작 시 랜덤
    nonce**(`secrets.token_hex`) + 클라이언트 nonce 일치 검사 + `hmac.compare_digest`.
- 비고: 본 인증은 LAN용 경량 가드. 외부 노출 시 TLS 리버스 프록시 권장(README/CLAUDE.md 명시).

## 2026-06-14 — GitHub 공개 준비 & 웹 설정 페이지

### 웹 설정 페이지 추가
- `config.yaml`을 직접 수정하지 않고 웹 UI에서 스트림/인코더/화질/오디오/버퍼/레이아웃/인증을
  편집하도록 설정 페이지(`web/settings.html`) 신설.
- **저장 모델**: `config.yaml`은 커밋용 기본값으로 두고, 런타임 변경은 `data/settings.json`에
  영속. `load()`가 `기본값 ← config.yaml ← settings.json` 순으로 병합. `config.save()`는
  편집 가능 키만 원자적으로 기록. 비밀번호는 변경 시에만 갱신, API 응답엔 절대 노출하지 않음
  (`password_set` 플래그만).
- **API**: `GET/POST /api/config`. POST는 저장 후 `streams.reconfigure()`로 실행 중인
  스트림을 정지해 새 파라미터를 강제 반영(프론트 재로드 시 활성 스트림 재시작).
- **프론트 개선**: 데이터 소스를 `/api/streams` → `/api/config`로 교체. `enabled=true`
  스트림만 그리드에 표시(= 설정에서 켠 개수만큼). `grid_columns`로 열 수 고정. hls.js 튜닝을
  설정 기반(`liveSyncDuration`=버퍼초, `maxLiveSyncPlaybackRate`=따라잡기 배속)으로 전환.

### GitHub 정리
- MIT `LICENSE`, `.gitignore`(`.venv`·캐시·`data/*`·`_hls/` 제외, `data/.gitkeep`만 추적),
  README에 설정 페이지·API·라이선스 추가, compose에 `./data` 볼륨.
- `git init` + 초기 커밋.
- 결정: `data/`는 통째로 무시하되 빈 디렉터리 유지를 위해 `data/*` + `!data/.gitkeep` 패턴
  사용(`data/`만으로는 예외가 안 먹힘).

## 2026-06-14 — 인코더 자동 감지 버그 수정

- 증상: 컨테이너(GPU 없음)에서 `encoder: auto`가 `h264_nvenc`로 선택돼 런타임에
  "Could not open encoder"로 실패.
- 원인: `ffmpeg -encoders`는 **컴파일된** 인코더를 나열할 뿐, 하드웨어 가용성을 보장하지 않음.
- 수정: `encoders.py`가 HW 인코더는 **실제 테스트 인코딩**(64x64 null 출력)으로 가용성을
  판정하도록 변경. SW(libx264)는 컴파일 목록만 신뢰. → Mac 네이티브는 videotoolbox, 컨테이너는
  libx264로 올바르게 폴백.

## 2026-06-14 — 오디오 "기계음" 디버깅

- 증상: 라이브 재생 시 음성이 기계음/끊김.
- 진단: 소스/재인코딩/복사 오디오를 스펙트로그램으로 비교 → **생성 파일 자체는 정상**. 즉 코덱
  손상이 아니라 라이브 재생 단계 문제로 판단.
- 수정 ①(백엔드): HLS 세그먼트를 키프레임에 정렬(`-g`=fps×hls_time + `-force_key_frames`)해
  세그먼트 경계 끊김 제거. `-fflags nobuffer` 제거(지터 완화), 오디오 포맷 고정 +
  `aresample=async=1`로 싱크 안정.
- 수정 ②(프론트): hls.js의 `lowLatencyMode`(라이브 엣지를 쫓다 스킵 → 기계음 주원인) 제거,
  `maxLiveSyncPlaybackRate`(부드러운 따라잡기) + 버퍼 확대로 전환.

## 2026-06-14 — 독립 프로젝트 시작

- 포털 에뮬레이터에 얹었던 라이브 뷰 기능을 분리해 `~/rtsp-live-viewer/` 독립 프로젝트로 시작.
- 구성: Flask 백엔드(`server/`: app·streams·encoders·config) + hls.js 프론트(`web/`).
- 다중 스트림 매니저(`streams.py`): 스트림별 ffmpeg 프로세스 → 스트림별 HLS 디렉터리, 유휴
  자동 정지(idle reaper), ad-hoc URL 재생.
- 인코더 4종 구현(libx264/videotoolbox/nvenc/qsv) + `auto`. 전체 브라우저(Chrome) 대상이라
  H.265→H.264 트랜스코딩.
- 멀티 에이전트로 백엔드/프론트/패키징 병렬 작성, 통합·검증.
