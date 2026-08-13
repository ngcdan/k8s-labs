# 01 · Docker first app — build image, run container, volume

Chương 1/2 của module Docker images. Kế tiếp: [02 · Multi-stage & registry](02-multistage-registry.md).

**Mục tiêu:** tự đúc image từ Dockerfile, hiểu **vì sao** image bất biến & layer-cache khiến build nhanh, chạy container có port + logs, dùng volume giữ data.
**Nền:** lab `docker-swarm` bạn dùng image dựng sẵn; buổi này lùi xuống nền — **tự đúc ra image đó**. Image chính là thứ Pod chạy ở các chặng K8s sau.
**⏱** 60–75 phút · **Sân:** host local (OrbStack), không cần cloud.

> Mỗi mục: **Chốt → Vì sao → Cơ chế → Dùng/không → Làm → Kết quả** (output để đối chiếu). Đọc để *hiểu*, gõ để *thấy*.

---

## 1. Image vs Container

**Chốt:** image là *khuôn* bất biến (read-only); container là *tiến trình* chạy từ khuôn đó, có thêm một lớp ghi-được riêng — từ 1 khuôn đúc được N nhà độc lập.

- **Image** = snapshot **read-only, bất biến**, đóng gói mọi thứ app cần để chạy.
- **Container** = tiến trình chạy từ image, **thêm một lớp ghi-được (writable) mỏng** phủ lên các lớp read-only.
- **1 image → N container độc lập:** sửa trong 1 container chỉ đổi lớp writable của nó, **không đụng** image hay container khác.
- Container **sống chừng nào tiến trình chính (PID 1) còn sống**.

**Vì sao:** "chạy trên máy tôi được, lên server lỗi thiếu thư viện". Máy dev có `libpq` 14, server có `libpq` 9 → app crash. Container đóng băng **app + toàn bộ userland** vào một đơn vị → chạy ở đâu cũng y hệt.

**Cơ chế:** container **không phải VM** — nó là một tiến trình Linux **cô lập** bằng 2 cơ chế kernel: `namespaces` (quyết định nó *nhìn thấy* gì: pid/network/mount riêng) và `cgroups` (quyết định nó *dùng được* bao nhiêu CPU/RAM). Mọi container **chung kernel host** → không boot OS, bật tính bằng mili-giây.

> **Ẩn dụ:** image = khuôn đúc nhà; container = căn nhà đúc ra (đập đi xây lại thoải mái); kernel host = mặt đất chung mọi nhà đứng trên.

| | Container | VM |
|---|---|---|
| Guest OS | Không (chung kernel host) | Có (nguyên OS) |
| Khởi động | mili-giây → giây | vài phút |
| Kích thước | MB (app + libs) | GB (kèm OS) |
| Cô lập | nhẹ (namespace/cgroup) | mạnh (hypervisor) |

**Dùng / không:** container hợp microservice, CI/CD, dev env. **Phản đề:** cần cô lập bảo mật cực cao (chạy code không tin tưởng) → kernel dùng chung là điểm yếu, phải VM/gVisor; cần chạy workload Windows trên host Linux → container không giúp (kernel khác).

**Làm:**
```bash
docker pull nginx:alpine
docker run -d --name nha1 nginx:alpine
docker run -d --name nha2 nginx:alpine
docker exec nha1 sh -c 'echo "nha1 đã sửa" > /usr/share/nginx/html/index.html'
docker exec nha1 cat /usr/share/nginx/html/index.html
docker exec nha2 cat /usr/share/nginx/html/index.html
```
**Kết quả:**
```text
$ docker exec nha1 cat /usr/share/nginx/html/index.html
nha1 đã sửa                                ← chỉ lớp writable của nha1 đổi

$ docker exec nha2 cat /usr/share/nginx/html/index.html
<!DOCTYPE html>
<head><title>Welcome to nginx!</title>     ← nha2 vẫn GỐC → container độc lập
```
→ **Verify:** nha1 đổi, nha2 + image không đổi. Dọn: `docker rm -f nha1 nha2`.

![[image-vs-container.excalidraw]]

---

## 2. Dockerfile & layer-cache

**Chốt:** Dockerfile là kịch bản xếp lớp; đổi 1 layer thì mọi layer *sau* nó phải build lại → xếp cái ít-đổi lên trên để tận dụng cache.

- **Layer:** mỗi `RUN`/`COPY`/`ADD` tạo **một layer** (một *changeset* ghi cái đã thêm/sửa/che so với layer dưới). `ENV`/`WORKDIR`/`EXPOSE`/`CMD` chỉ là **metadata**, không tạo layer filesystem.
- **Cache:** build lại mà (instruction + đầu vào) không đổi → Docker **tái dùng layer**, bỏ qua bước đó.
- **Bust cache lan xuống:** một layer đổi → **mọi layer sau đều chạy lại**, kể cả không đổi.

**Vì sao:** sửa 1 dòng code mà phải đợi `npm install`/`apt-get` chạy lại 2 phút là vô lý. Hiểu layer-cache = build từ vài phút xuống vài giây.

**Cơ chế:** Docker checksum đầu vào mỗi bước. Với `COPY` → checksum **nội dung file**; đổi file → digest mới → rebuild. Với `RUN` → chỉ so **chuỗi lệnh** (không so kết quả) → `apt-get update` cần `--no-cache` khi muốn làm mới. Địa chỉ mỗi layer là `sha256` của nội dung (*content-addressable*) → cùng nội dung = cùng digest = lưu 1 bản, nhiều image chia sẻ.

> **Ẩn dụ:** layer-cache như checkpoint trong game — chưa đổi gì từ checkpoint thì load thẳng; đổi một thứ ở giữa → mọi checkpoint sau nó bị huỷ, phải chạy lại từ đó.

**Dùng / không:** tách `COPY package.json` + `RUN npm install` **trước**, `COPY . .` **sau** → sửa code không cài lại dependency; `.dockerignore` loại `node_modules` để context nhỏ, checksum ổn định. **Phản đề:** với project cực nhỏ (1 file, không dependency) thì tối ưu thứ tự layer gần như không đáng kể — đừng phức tạp hoá.

**Làm** — tạo **4 file** trong thư mục app (dùng editor hoặc terminal):
```bash
mkdir -p ~/dev/k8s-labs/01-docker-images/app/first-app && cd ~/dev/k8s-labs/01-docker-images/app/first-app
```
**`server.js`** — app Node tối thiểu:
```js
const http = require('http');
const port = process.env.PORT || 3000;
http.createServer((_, res) => res.end('First app image — OK\n'))
  .listen(port, () => console.log('listening on ' + port));
```
**`package.json`** — khai báo `npm start`:
```json
{ "name": "nodeapp", "version": "1.0.0", "scripts": { "start": "node server.js" } }
```
**`node.dockerfile`** — công thức build (comment ở **dòng riêng** phía trên mỗi lệnh; Dockerfile **không** nhận comment cuối dòng):
```dockerfile
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
```
> **Gotcha:** Dockerfile chỉ nhận comment ở **đầu dòng** (`# ...`). Viết comment **cuối dòng lệnh** (vd `ENV PORT=3000 # ghi chú`) sẽ lỗi `can't find = in "#"` — parser hiểu `#...` là một token `key=value`.
**`.dockerignore`** — chặn `node_modules` khỏi build context:
```text
node_modules
```
**Kết quả:**
```text
$ ls -a
.  ..  .dockerignore  node.dockerfile  package.json  server.js
```
→ **Verify:** đủ 4 file (`.dockerignore` ẩn — cần `ls -a`).

![[layer-cache.excalidraw]]

---

## 3. docker build — layer & cache tận mắt

**Chốt:** `docker build -t <tên> <context>` biến Dockerfile thành image; build lần 2 chỉ chạy lại từ layer bị đổi trở xuống. Image **immutable** — "update" = build tag mới.

- Dấu **`.`** cuối = *build context* (thư mục gửi cho daemon; thiếu → lỗi "requires exactly one argument").
- File tên khác `Dockerfile` → thêm **`-f <file>`**.
- Tag để push registry: `<user>/<image>:<version>`.

**Vì sao:** "làm sao update image?" — câu hỏi bẫy. Image bất biến; không sửa image cũ, chỉ **build image mới + tag mới** (rồi trỏ deployment sang). Dùng `:latest` cho mọi thứ dễ gây breaking change vì không biết đang chạy bản nào.

**Cơ chế:** build đọc Dockerfile từ trên xuống, mỗi bước tạo *intermediate image* làm cache. Log build đánh số `[n/m]` từng layer; lần sau thấy `CACHED` là tái dùng.

**Làm:**
```bash
cd ~/dev/k8s-labs/01-docker-images/app/first-app
docker build -t nodeapp .                      # (1) sai — không có file tên "Dockerfile"
docker build -t nodeapp -f node.dockerfile .   # (2) đúng
docker images nodeapp
echo "// đổi 1 dòng" >> server.js
docker build -t nodeapp -f node.dockerfile .   # (3) build lại — xem CACHED
```
**Kết quả (1) — lỗi thiếu Dockerfile:**
```text
ERROR: failed to solve: failed to read dockerfile:
  open Dockerfile: no such file or directory
```
**Kết quả (2) — build lần đầu, mọi layer chạy thật:**
```text
 => [1/4] FROM docker.io/library/node:alpine       3.2s
 => [2/4] WORKDIR /var/www                          0.1s
 => [3/4] COPY package.json ./                      0.0s
 => [4/4] RUN npm install                           1.4s
 => => naming to docker.io/library/nodeapp:latest
```
**Kết quả (3) — build lại sau khi sửa `server.js`:**
```text
 => CACHED [1/4] FROM ...node:alpine
 => CACHED [2/4] WORKDIR /var/www
 => CACHED [3/4] COPY package.json ./
 => CACHED [4/4] RUN npm install     ← cache, KHÔNG cài lại dependency
 => [5/5] COPY . .                   ← chỉ bước này chạy lại (source đổi)
```
→ **Verify:** lần 3 thấy `CACHED` ở 4 bước đầu, chỉ `COPY . .` chạy lại — đúng nguyên tắc thứ tự layer (mục 2).

---

## 4. Registry — push & pull; tag vs digest *(tùy chọn, cần Docker Hub)*

**Chốt:** node/máy khác chỉ chạy được image nếu nó nằm trên **registry**; **tag** là con trỏ mềm (đổi được), **digest `@sha256`** là vân tay bất biến — prod nên pin digest.

- Tên image = `[registry/]<user>/<image>:<tag>`. Không có domain/IP ở đầu → Docker mặc định **Docker Hub**; có domain → registry đó:

| Tên image | Đẩy/kéo tới đâu |
|---|---|
| `nqcdan/nodeapp:1.0` | Docker Hub (thực chất `docker.io/nqcdan/nodeapp:1.0`) |
| `ghcr.io/nqcdan/nodeapp:1.0` | GitHub Container Registry |
| `registry.example.com/nqcdan/nodeapp:1.0` | Registry riêng (self-hosted) |

- `docker login` (bật 2FA → **Access Token** thay password) → `docker push` (đẩy từng layer; layer đã có trên registry thì bỏ qua).

**Vì sao:** deploy tag `:latest` → không biết đang chạy build nào, rollback vô nghĩa (revision cũ vẫn trỏ cùng tag). Pin **git-sha/digest** → mỗi build có tên duy nhất, rollback xác định.

> **Ẩn dụ:** registry = kho NPM của team — build xong `publish`, mọi máy `install` từ cùng chỗ, không copy `.tgz` qua SSH.

**Làm** (đổi `<user>` = username Docker Hub):
```bash
docker build -t <user>/nodeapp:1.0 -f node.dockerfile .
docker login
docker push <user>/nodeapp:1.0
```
**Kết quả:**
```text
$ docker push <user>/nodeapp:1.0
The push refers to repository [docker.io/<user>/nodeapp]
5f70bf18a086: Pushed
1.0: digest: sha256:9c8f... size: 1780        ← digest = vân tay bất biến của image
```
→ **Verify:** hub.docker.com thấy repo `<user>/nodeapp:1.0`. Máy khác: `docker pull <user>/nodeapp:1.0`. **Dùng/không:** `:latest` chấp nhận được khi pull *tool* trong CI (muốn bản mới nhất); KHÔNG ổn ở *điểm deploy artifact của bạn*.

---

## 5. docker run — port, vòng đời, exit code

**Chốt:** `docker run` thêm lớp writable + tạo tiến trình PID 1 trong container; container sống theo PID 1, và **exit code cho biết nó chết thế nào**.

- **`-p 8080:80`** — map cổng ngoài (host) → cổng trong (container).
- **`-d`** — chạy nền; **`--name`** — đặt tên (không có → tên ngẫu nhiên).
- `docker ps [-a]` (đang chạy / cả đã dừng) · `stop` · `rm` (xóa container, **không đụng image**) · `rmi` (xóa image).

**Vì sao:** rolling update K8s gửi tín hiệu tắt pod cũ; nếu PID 1 không bắt SIGTERM, sau grace period bị **SIGKILL** → `exit 137`, request dở dang. Hiểu vòng đời = deploy không mất data.

**Cơ chế:** kernel đối xử PID 1 đặc biệt — **không có signal handler mặc định**: app không tự bắt SIGTERM thì tín hiệu bị bỏ qua → `docker stop` chờ ~10s rồi SIGKILL.

| exit | nghĩa | thường do |
|---|---|---|
| `0` | thoát sạch | app shutdown đúng |
| `143` | 128+15 (SIGTERM) | bắt SIGTERM, exit gọn |
| `137` | 128+9 (SIGKILL) | grace period hết **hoặc** OOM |
| `127` | command not found | `ENTRYPOINT`/`CMD` sai |

**Dùng / không:** app tự bắt SIGTERM (Go/Node có handler) thì đủ. **Phản đề:** shell làm PID 1 (`ENTRYPOINT ["sh","start.sh"]`) không forward tín hiệu xuống app con → dùng `exec` trong script, hoặc `tini` làm PID 1 (reap zombie + forward signal).

**Làm:**
```bash
docker run -p 8080:80 -d --name web nginx:alpine
docker ps
curl -s localhost:8080 | head -3
docker stop web
docker ps -a
docker rm web
docker images nginx
```
**Kết quả — `docker ps`:**
```text
CONTAINER ID   IMAGE          STATUS         PORTS                  NAMES
ce4b1f2a9d33   nginx:alpine   Up 3 seconds   0.0.0.0:8080->80/tcp   web
```
**`curl localhost:8080` → trang nginx; sau `docker stop web` → `docker ps -a`:**
```text
ce4b1f2a9d33   nginx:alpine   Exited (0) 2 seconds ago   web    ← còn "xác", exit 0
```
**Sau `docker rm web` → `docker images nginx`:**
```text
nginx   alpine   1e5f3c76a…   22MB      ← image VẪN còn dù container đã xóa
```
→ **Verify:** `-p` map đúng; stop → `Exited (0)`; rm container không đụng image.

---

## 6. docker logs — chẩn đoán khi container chết

**Chốt:** `docker logs <id>` đọc log **cả khi container đã dừng/crash** — là lệnh đầu tiên khi container chết mà `docker ps` trống.

**Vì sao:** chạy `-d`, container crash → `docker ps` không thấy gì. Không biết `docker logs` là **bế tắc**: không rõ vì sao chết. (Phản xạ này dùng y hệt trong K8s: `kubectl logs [--previous]`.)

**Cơ chế:** Docker giữ stdout/stderr của container trong log driver; `logs` đọc lại được kể cả container `Exited`.

**Làm** (tái hiện crash bằng app tự thoát lỗi):
```bash
cd ~/dev/k8s-labs/01-docker-images/app/first-app
echo "console.error('FATAL: cannot connect to mongodb://db:27017'); process.exit(1);" > crash.js
printf 'FROM node:alpine\nWORKDIR /var/www\nCOPY crash.js ./\nENTRYPOINT ["node","crash.js"]\n' > crash.dockerfile
docker build -t crashapp -f crash.dockerfile .
docker run -d --name boom crashapp
docker ps
docker ps -a
docker logs boom
docker rm boom
```
**Kết quả:**
```text
$ docker ps
CONTAINER ID   IMAGE   STATUS   NAMES           ← rỗng: không có gì đang chạy

$ docker ps -a
7a2f9c1b8e40   crashapp   Exited (1) 4 seconds ago   boom   ← exit code 1

$ docker logs boom
FATAL: cannot connect to mongodb://db:27017     ← lý do crash, dù ps đã không thấy
```
→ **Verify:** `ps` trống nhưng `logs boom` in dòng lỗi → biết nguyên nhân.

---

## 7. Volumes — giữ data ngoài container

**Chốt:** file ghi trong container mất khi container bị xóa; **volume** `-v <host>:<container>` map thư mục host vào container → data sống độc lập vòng đời container.

- **Dev:** trỏ vào web-root/source host → sửa file thấy ngay, **không rebuild image**.
- **Prod:** log/db ghi ra host → **persist dù container chết**.
- `$(pwd)` (Mac/Linux) hoặc `${PWD}` (Windows PowerShell).

**Vì sao:** container là *cattle* (dùng-rồi-vứt) — mọi thứ ghi bên trong biến mất khi bị thay. State quan trọng (DB, log, upload) phải nằm **ngoài** container.

**Cơ chế:** volume "mount đè" một thư mục host lên đường dẫn trong container; container mới ghi cùng đường dẫn sẽ **thấy lại data cũ** trên host. Đây là *host volume* (gắn với máy chạy container).

> **Ẩn dụ:** container = phòng khách sạn (dọn sạch sau mỗi khách); volume = két an toàn gắn tường — đổi khách, đồ trong két vẫn còn.

**Dùng / không:** dev live-edit + prod log/db. **Phản đề:** host volume gắn chặt vào 1 máy — host chết vẫn mất; cần bền/chia sẻ nhiều node → dùng volume mạng (NFS) hoặc (ở K8s) PV/PVC trên Ceph.

**Làm** (dev — override trang nginx bằng folder host):
```bash
mkdir -p ~/dev/k8s-labs/01-docker-images/site && cd ~/dev/k8s-labs/01-docker-images/site
echo '<h1>Hello from my custom page</h1>' > index.html
docker run -p 8080:80 -d --name devweb -v $(pwd):/usr/share/nginx/html nginx:alpine
curl -s localhost:8080
echo '<h1>Đã sửa — không build lại</h1>' > index.html
curl -s localhost:8080
docker rm -f devweb
```
**Kết quả:**
```text
$ curl -s localhost:8080
<h1>Hello from my custom page</h1>        ← nginx phục vụ file TỪ HOST, không phải từ image

# sau khi sửa index.html trên host:
$ curl -s localhost:8080
<h1>Đã sửa — không build lại</h1>          ← đổi NGAY, không build/khởi động lại
```
→ **Verify:** sửa file host → nội dung đổi tức thì.

## Dọn dẹp
```bash
docker rm -f web devweb boom nha1 nha2 2>/dev/null
docker rmi nodeapp crashapp 2>/dev/null
```

---

## Đủ khi (nói trơn bằng lời mình)
① image vs container (+ vì sao không phải VM) · ② layer-cache + vì sao thứ tự Dockerfile quyết định tốc độ · ③ image immutable, "update" = tag mới · ④ tag vs digest · ⑤ exit 137 nghĩa gì · ⑥ `docker logs` khi `ps` trống · ⑦ volume dev vs prod + vì sao container là cattle.

## Recall — tự kiểm (cuối buổi)
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

1. Container khác VM ở điểm cốt lõi nào, vì sao bật nhanh hơn nhiều?
2. Vì sao `COPY package.json` + `RUN npm install` đặt TRƯỚC `COPY . .`?
3. `ENV`/`WORKDIR`/`EXPOSE` có tạo layer filesystem không?
4. "Làm sao update một image?" — trả lời & vì sao?
5. Tag khác digest thế nào? Prod nên pin cái nào?
6. `-d` và `-p 8080:80` nghĩa là gì?
7. `exit 137` luôn là OOM không?
8. Container `-d` crash, `docker ps` trống — làm sao biết vì sao?
9. Xóa container thì image còn không?
10. Volume `-v` giải quyết gì? Vì sao host volume vẫn có thể mất data?

### Đáp án

1. Container **không chạy Guest OS** — chỉ là tiến trình Linux cô lập bằng namespaces/cgroups, chung kernel host; không boot OS nên bật mili-giây→giây. VM phải boot nguyên OS trên hypervisor → vài phút, vài GB.
2. Layer đổi → mọi layer sau build lại. Dependency ít đổi để trên (cache), source hay đổi để dưới → sửa code không cài lại dependency.
3. Không — metadata, không tạo layer filesystem. Chỉ `RUN`/`COPY`/`ADD` tạo layer.
4. Không update — image immutable; build image mới + tag mới rồi trỏ deployment sang. `:latest` cho mọi thứ dễ gây breaking change.
5. Tag = con trỏ mềm (đổi được); digest `@sha256` = vân tay nội dung, bất biến. Prod pin **digest** (hoặc tag version cụ thể).
6. `-d` chạy nền (không lock console). `-p 8080:80` map cổng ngoài 8080 → cổng 80 container.
7. Không. 137 = SIGKILL, đến từ **OOM** (hết RAM) *hoặc* **grace period hết** (app không bắt SIGTERM). Phân biệt: `describe`/`dmesg` xem có OOMKilled không.
8. `docker logs <id>` — đọc cả khi Exited; in dòng lỗi nguyên nhân.
9. Còn. `docker rm` chỉ xóa container; image vẫn trong `docker images`. Xóa image: `docker rmi <id>`.
10. Giữ data ngoài vòng đời container (dev live-edit; prod persist). Là *host volume* → gắn 1 máy; host chết vẫn mất → cần volume mạng / PV-PVC để bền hơn.

---

## Bắc cầu sang Kubernetes
image (immutable, tag/digest) → `spec.containers[].image` (pin digest an toàn nhất) · `docker run -p` → Pod + Service · PID 1/SIGTERM → `terminationGracePeriodSeconds` + graceful shutdown · `docker logs` → `kubectl logs [--previous]` · `-v` host volume → Volume/PV/PVC (bền, tách khỏi node). Image bạn đúc ở đây chính là thứ Pod sẽ chạy.

---

## Nguồn & xem lại
- Chương kế: [02 · Multi-stage & registry](02-multistage-registry.md) · tự kiểm: [notes.md](notes.md).
