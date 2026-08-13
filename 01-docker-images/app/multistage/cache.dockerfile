FROM node:alpine
WORKDIR /app
# copy dep TRƯỚC → ít đổi → cache bền
COPY package.json ./
RUN npm install
# source SAU → hay đổi → cache tính từ đây
COPY . .
CMD ["node", "-e", "console.log(1)"]
