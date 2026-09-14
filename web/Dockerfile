FROM node:22-alpine

WORKDIR /app

# Dependencies first, so editing source does not reinstall the tree.
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
