FROM python:3.12-slim
WORKDIR /app
COPY servidor.py index.html ./
ENV PORT=8000
EXPOSE 8000
CMD ["python", "servidor.py"]
