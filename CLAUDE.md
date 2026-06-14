# CLAUDE.md — rtsp-live-viewer

이 파일은 Claude(및 기여자)가 이 저장소에서 작업할 때 참고하는 가이드다.

## 개요
RTSP/RTMP/HTTP 영상 소스를 **ffmpeg로 H.264/HLS로 실시간 변환**해 브라우저에서 다채널로 보는
"재생 전용" 뷰어. 백엔드 Flask, 프론트 hls.js. SigmaStar 사이니지 장비 없이 그 장비가 받던
RTSP를 화면으로 보기 위해 시작됨(자세한 경위는 `docs/DEVLOG.md`).

## 실행
- 도커: `docker compose up -d --build` → http://localhost:8081 (8080 충돌 회피용 포트)
- 네이티브(모든 HW 인코더 사용 가능): `.venv/bin/python server/app.py` (기본 80, `RLV_PORT`로 변경)
- 의존: Python 3.12 + Flask + PyYAML, 시스템 `ffmpeg`.

## 구조
```
server/
  app.py       Flask 라우트, digest 인증, 정적 서빙, /api/* 와 /hls/* 
  streams.py   다중 스트림 ffmpeg 매니저(id→proc), HLS 디렉터리, 유휴 자동정지, _build_cmd
  encoders.py  인코더 감지/선택, 인코더별 -c:v 인자
  config.py    config.yaml ← data/settings.json 병합 로드 + save()
web/
  index.html   뷰어(그리드+포커스 모달, /api/config 기반), 인라인 JS
  settings.html 설정 페이지, 인라인 JS
  css/, js/hls.min.js(벤더링)
config.yaml    기본값(커밋). 런타임 변경은 data/settings.json(gitignore)
```

## 설정 모델 (중요)
- **우선순위**: 내장 기본값 ← `config.yaml`(커밋된 기본값) ← `data/settings.json`(런타임, gitignore).
- 웹 설정 페이지는 `POST /api/config` → `config.save()`로 **`data/settings.json`에만** 기록.
  `config.yaml`은 절대 런타임에 수정하지 않는다. `data/settings.json`은 커밋 금지(.gitignore).
- 편집 가능 키는 `config.EDITABLE_KEYS` 참조. 비밀번호는 API 응답에 노출 금지(`password_set`만).

## 핵심 규칙 / 함정
- **인코더 감지**: `ffmpeg -encoders`는 컴파일된 목록일 뿐 HW 가용성을 보장하지 않는다.
  `encoders.py`는 HW 인코더(nvenc/qsv/videotoolbox)를 **실제 테스트 인코딩**으로 판정한다.
  새 인코더 추가 시 이 probe 경로를 따를 것.
- **라이브 오디오/영상 매끄러움**: HLS 세그먼트를 키프레임에 정렬(`_build_cmd`의 `-g`=fps×hls_time
  + `-force_key_frames`)하고, 프론트는 `liveSyncDuration`(버퍼 초)+`maxLiveSyncPlaybackRate`로
  튜닝한다. `lowLatencyMode`/고정 `liveSyncDurationCount`는 끊김(기계음) 유발 — 쓰지 말 것.
- **인증**: digest, nonce는 **프로세스 시작 시 랜덤**(`secrets.token_hex`), 비교는
  `hmac.compare_digest`. 인증을 켜면 `/hls/`도 보호된다(브라우저가 realm 자격증명 재사용).
  이는 LAN용 경량 가드이며, 외부 노출 시 **TLS 리버스 프록시 뒤**에 둘 것.
- **HW 인코더 + 도커**: 컨테이너 기본 ffmpeg는 SW만. VideoToolbox는 컨테이너 불가(Mac 네이티브),
  NVENC는 `--gpus`, QSV는 `/dev/dri` 패스스루 필요.
- 포트: 도커는 8081(다른 프로젝트 8080과 충돌 회피).

## API
- `GET /api/config`, `POST /api/config` — 런타임 설정 조회/저장
- `GET /api/streams`, `/api/streams/<id>/start|stop|status`, `/api/play?url=`
- `GET /hls/<id>/index.m3u8`(+ `.ts`) — HLS

## 검증 방법
- 설정: `curl /api/config`, `POST /api/config`로 라운드트립 확인 후 `data/settings.json` 점검.
- 재생: 스트림 start → `/hls/<id>/index.m3u8` 200 + 최신 `.ts` 바이트>0.
- UI: 헤드리스 크롬 스크린샷(영상 픽셀은 헤드리스 합성 제약 — 타임라인 진행/요청 로그로 확인).

## 작업 규칙
- 코드 스타일: 간결한 영어 주석, 기존 모듈과 일관. stdlib + Flask + PyYAML만.
- `config.yaml`의 실제 카메라 IP/계정은 환경별 값 — 공개 시 예시값으로 일반화 고려.
- 커밋/푸시는 사용자가 요청할 때만.
