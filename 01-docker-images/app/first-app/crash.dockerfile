FROM node:alpine
WORKDIR /var/www
COPY crash.js ./
ENTRYPOINT ["node","crash.js"]
