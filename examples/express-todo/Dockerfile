FROM node:20-slim
WORKDIR /app
COPY package.json .
RUN npm install --omit=dev
COPY . .
CMD ["node","server.js"]
