FROM debian:stable-slim
RUN apt-get update && apt-get install -y curl
RUN apt-get remove -y curl && apt-get clean && rm -rf /var/lib/apt/lists/*
