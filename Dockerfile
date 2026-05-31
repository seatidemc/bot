FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir nonebot2[fastapi] nonebot-adapter-onebot httpx aiosqlite

COPY bot.py .
COPY src/ src/

RUN mkdir -p /app/data

CMD ["python", "bot.py"]
