const http = require('http');
const port = process.env.PORT || 3000;

http.createServer((_, res) => res.end('First app image — OK\n')).listen(port,
  () => console.log('listening on ' + port));

// đổi 1 dòng
