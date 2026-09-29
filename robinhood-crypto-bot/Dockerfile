FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rhbot ./rhbot
RUN useradd --create-home bot && mkdir /data && chown bot /data
USER bot
ENV RHBOT_DATA=/data PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "rhbot"]
CMD ["trade", "--interval", "1h", "--strategy", "trend"]
