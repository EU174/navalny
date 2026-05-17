FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    aiohttp \
    beautifulsoup4 \
    lxml

COPY bot/ bot/
COPY main.py .

ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "-u", "main.py"]
