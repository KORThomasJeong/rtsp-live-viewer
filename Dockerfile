FROM python:3.12-slim

# ffmpeg 설치(트랜스코딩 필수) 후 apt 캐시 정리로 이미지 용량 축소.
# 주의: 컨테이너 기본 ffmpeg는 소프트웨어 인코딩(libx264)만 동작한다.
#       HW 인코더(h264_videotoolbox/h264_nvenc/h264_qsv)는 호스트 디바이스
#       패스스루가 필요하며, h264_videotoolbox는 컨테이너에서 사용 불가하다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시 활용).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스/정적파일/기본 설정 복사.
COPY server/ /app/server/
COPY web/ /app/web/
COPY config.yaml /app/config.yaml

# 컨테이너 내부 리슨 포트(환경변수 RLV_PORT로 변경 가능).
ENV RLV_PORT=80
EXPOSE 80

CMD ["python", "server/app.py"]
