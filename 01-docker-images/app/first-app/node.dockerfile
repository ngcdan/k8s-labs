# base image (prod nên pin: node:22-alpine)
FROM node:alpine
# biến môi trường (metadata, không tạo layer)
ENV PORT=3000
# thư mục làm việc trong image
WORKDIR /var/www
# copy dependency TRƯỚC → tận dụng cache
COPY package.json ./
RUN npm install
# copy source SAU (hay đổi)
COPY . .
# khai báo cổng
EXPOSE $PORT
ENTRYPOINT ["npm", "start"]