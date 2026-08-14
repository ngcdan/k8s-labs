# 02 · Multi-stage build, tối ưu image & registry (tag/digest)

Chương 2/2 của module Docker images. Trước: [01 · Docker first app](01-first-app.md) · kế tiếp: module k8s-pod.

**Mục tiêu:** hiểu vì sao image production phải nhỏ & sạch; đọc được quan hệ layer↔size (kể cả bẫy "xóa file không giảm size"); dùng **multi-stage build** để tách builder nặng khỏi runtime gọn; sắp xếp Dockerfile cho build-cache hiệu quả; và phân biệt **tag vs digest** khi kéo image từ registry.
**Nền:** tiếp lab *Docker first app* (đã biết build/run/layer-cache cơ bản). Đây là "nâng cấp" cách đúc image sao cho gọn + an toàn — chuẩn để build image cho catalog sau này.

---

## 1. Layer ↔ image size — bẫy "xóa file không giảm size"

**Chốt:** xóa file ở một layer *sau* chỉ che nó khỏi container, nhưng bytes vẫn nguyên ở layer cũ và vẫn tính vào tổng size.

- **Image RootFS** = các content layer chồng lên nhau; tổng size = cộng dồn mọi layer.
- **Content layer** = chỉ `RUN`, `COPY`, `ADD` tạo; `ENV`/`EXPOSE`/`USER`/`CMD` chỉ là metadata, không tạo layer.
- **Whiteout** = kỹ thuật union filesystem che file đã xóa khỏi view của container, nhưng layer gốc (kèm bytes) vẫn còn nguyên.

**Vì sao:** dev hay cài tool rồi `RUN rm` để "dọn" — nghĩ image nhỏ lại. Thực ra `docker images` vẫn báo size y hệt (đôi khi còn nhỉnh hơn vì layer whiteout). Không biết bẫy này → image prod phình to, pull chậm, attack surface lớn.

**Cơ chế:** mỗi layer là một *changeset* (thêm/sửa/che so với layer dưới). Khi `RUN rm /big` ở layer 3, union FS thêm một file `.wh./big` (whiteout marker) để che file gốc ở layer 2 — nhưng layer 2 không mất. `docker history` in từng layer kèm SIZE thật, cho thấy layer "dd …" vẫn giữ 50 MB dù container không thấy file đó nữa.

> **Ẩn dụ:** sách in sẵn (layer 2 có trang 10 nội dung "bí mật"); layer 3 dán miếng giấy đen phủ trang đó — người đọc không thấy, nhưng trang 10 vẫn nằm trong cuốn sách, cuốn sách vẫn dày y cũ.

**Dùng / không:** bao giờ cần thêm-rồi-dọn trong cùng Dockerfile → làm trong **cùng một `RUN`** (nối `&&`). **Phản đề:** nếu file cần giữ lại qua nhiều bước debug/test thì tách layer là chấp nhận được *trong stage builder*; chỉ stage cuối (runtime) mới cần nhỏ — và đó chính là lý do có multi-stage (mục 4).

**Làm:**
```bash
mkdir -p ~/dev/k8s-labs/01-docker-images/app/multistage && cd ~/dev/k8s-labs/01-docker-images/app/multistage

# image A: tạo file 50 MB rồi xóa ở layer SAU
cat > waste.dockerfile <<'EOF'
FROM alpine
RUN dd if=/dev/zero of=/big bs=1M count=50
RUN rm /big
EOF

docker build -t waste -f waste.dockerfile .
docker images waste
docker history waste
docker rmi waste
```

**Kết quả:**
```text
$ docker images waste
REPOSITORY   TAG       IMAGE ID       CREATED          SIZE
waste        latest    3f8a...        5 seconds ago    52.4MB     ← vẫn 50 MB+ dù /big đã rm

$ docker history waste
IMAGE          CREATED         CREATED BY                   SIZE
3f8a...        5s ago          RUN rm /big                  0B       ← whiteout: 0B
<missing>                      RUN dd if=/dev/zero ...       52.4MB  ← bytes vẫn còn đây
<missing>                      /bin/sh -c #(nop) ...         0B
```
→ **Verify:** `SIZE` cột `docker images` ≈ 52 MB; `docker history` thấy layer `dd` vẫn 52.4 MB — đúng bẫy whiteout.

---

## 2. Giảm nội dung image — 3 đòn bẩy

**Chốt:** image nhỏ nhờ 3 đòn: chọn base tối giản, gộp add + dọn vào cùng một `RUN`, và `.dockerignore` cắt build context.

- **Base tối giản:** `alpine` (~5 MB), `debian:slim`, `distroless`, chainguard — chỉ chứa đúng cái app cần chạy.
- **Gộp `RUN`:** thêm + xóa + dọn trong 1 bước → không có rác kẹt layer cũ.
- **`.dockerignore`:** loại `node_modules`, `.git`, file test → context nhỏ, checksum ổn định, build nhanh hơn.

**Vì sao:** image 1 GB kéo về mỗi lần deploy → chậm, tốn băng thông, và mỗi package thừa là một attack vector tiềm năng. Giảm image = giảm cả thời gian pull lẫn bề mặt tấn công.

**Cơ chế:** `apt-get update` để lại cache index trong `/var/lib/apt/lists/`; `apt-get clean` + `rm -rf /var/lib/apt/lists/*` xóa nó — nhưng chỉ có tác dụng nếu nằm **trong cùng một `RUN`** với lệnh install. Nếu ở `RUN` riêng → cache index đã bị đóng băng vào layer install, không xóa được.

> **Ẩn dụ:** nấu ăn và rửa bát trong cùng một lần (1 `RUN`) → bếp sạch; nấu xong đóng gói bếp bẩn (layer 1), lần sau mới rửa (layer 2) → rác đã bị đóng gói vào hộp rồi, hộp vẫn nặng.

| Cách | Hiệu quả | Ghi chú |
|---|---|---|
| Base `alpine` thay `ubuntu` | Tiết kiệm ~70 MB base | Thiếu glibc → app glibc cần `musl` hoặc `debian:slim` |
| Gộp `install + clean` trong 1 `RUN` | Loại cache apt khỏi layer | Dòng dài → dùng `\` xuống dòng |
| `.dockerignore` | Context nhỏ, checksum đúng | Quan trọng với `node_modules` |

**Dùng / không:** luôn áp với image production. **Phản đề:** trong CI pipeline dùng image builder để chạy test → kích cỡ ít quan trọng hơn tốc độ cài dep; không cần ép nhỏ stage builder, chỉ ép stage runtime.

**Làm:**
```bash
cd ~/dev/k8s-labs/01-docker-images/app/multistage

# SAI: cài rồi xóa ở RUN khác → rác kẹt layer cũ
cat > bad.dockerfile <<'EOF'
FROM debian:stable-slim
RUN apt-get update && apt-get install -y curl
RUN apt-get remove -y curl && apt-get clean && rm -rf /var/lib/apt/lists/*
EOF

# ĐÚNG: add + dọn trong 1 RUN
cat > good.dockerfile <<'EOF'
FROM debian:stable-slim
RUN apt-get update \
    && apt-get install -y curl \
    && apt-get remove -y curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
EOF

docker build -t sz-bad  -f bad.dockerfile  .
docker build -t sz-good -f good.dockerfile .
docker images | grep -E 'sz-bad|sz-good'
docker rmi sz-bad sz-good
```

**Kết quả:**
```text
$ docker images | grep -E 'sz-bad|sz-good'
sz-bad    latest   a1b2...   2m ago   104MB   ← curl layer còn đóng băng dù đã remove
sz-good   latest   c3d4...   1m ago    78MB   ← add + dọn cùng layer → nhỏ hơn 26 MB
```
→ **Verify:** `sz-good` nhỏ hơn `sz-bad` — gộp `RUN` thật sự giảm size.

---

## 3. Build-cache & thứ tự instruction

**Chốt:** cache vỡ ở bước N thì mọi bước từ N trở xuống phải chạy lại — vì vậy đặt cái ít-đổi lên trên, hay-đổi xuống dưới.

- **Cache hit:** instruction không đổi + (với `COPY`/`ADD`) checksum file không đổi → Docker **tái dùng layer**, bỏ qua bước đó.
- **Cache miss:** 1 bước đổi → bước đó và **toàn bộ bước sau** bị rebuild.
- **`RUN` gotcha:** Docker chỉ so **chuỗi lệnh**, không so kết quả — `apt-get update` dùng cache cũ kể cả có bản mới trên repo. Ép làm mới: `docker build --no-cache`.

**Vì sao:** sửa 1 dòng code mà phải đợi `npm install` chạy lại 2 phút là lãng phí. Tách `COPY package.json` + `RUN npm install` lên trước `COPY . .` → chỉ khi `package.json` đổi mới cài lại dep.

**Cơ chế:** build đọc Dockerfile từ trên xuống; mỗi step tạo *intermediate image* lưu theo content-hash. `COPY` → Docker hash nội dung file thật. `RUN` → hash chuỗi lệnh. Khi hash khớp → in `CACHED`, bỏ qua. Khi hash khác → rebuild, **vô hiệu hóa cache mọi step tiếp theo** kể cả chúng không đổi.

> **Ẩn dụ:** xây nhà theo tầng — mỗi tầng là checkpoint. Sửa tầng 3 → phải đập và xây lại tầng 3, 4, 5… Tầng 1-2 (móng, cột — ít đổi) giữ nguyên. Đừng để "sơn tường" (source code) ở tầng 1.

**Dùng / không:** tách `COPY <dep-file>` + install lên đầu luôn. **Phản đề:** với project không có dependency file rõ ràng (script Python đơn lẻ) thì tối ưu này không áp dụng được; cứ `COPY . .` + `RUN` thẳng cho đơn giản.

**Làm:**
```bash
cd ~/dev/k8s-labs/01-docker-images/app/multistage
echo '{ "name":"t","version":"1.0.0" }' > package.json

cat > cache.dockerfile <<'EOF'
FROM node:alpine
WORKDIR /app
# copy dep TRƯỚC → ít đổi, cache bền
COPY package.json ./
RUN npm install
# source SAU → hay đổi, cache tính từ đây
COPY . .
CMD ["node", "-e", "console.log(1)"]
EOF

docker build -t cachedemo -f cache.dockerfile .

# đổi source, KHÔNG đổi package.json
echo "console.log('x')" > app.js
docker build -t cachedemo -f cache.dockerfile .
docker rmi cachedemo
```

**Kết quả — lần 2 (sau khi đổi `app.js`):**
```text
 => CACHED [2/4] WORKDIR /app
 => CACHED [3/4] COPY package.json ./
 => CACHED [4/4] RUN npm install            ← không cài lại dep
 => [5/5] COPY . .                          ← chỉ bước này chạy lại (source đổi)
```
→ **Verify:** bước `RUN npm install` in `CACHED` — đúng: source đổi nhưng dep không cài lại.

---

## 4. Multi-stage build — builder nặng, runtime gọn

**Chốt:** `FROM ... AS <stage>` + `COPY --from=<stage>` cho phép build trong image nặng rồi chỉ chuyển artifact sang image nhỏ — chỉ stage cuối thành image final.

- **Multi-stage:** 1 Dockerfile, nhiều `FROM ... AS <name>`; stage sau `COPY --from=<name>` lấy file từ stage trước.
- **Stage cuối duy nhất thành image:** mọi stage trước (kể cả builder nặng với toàn bộ toolchain) bị bỏ lại.
- **`--target <stage>`:** build tới stage chỉ định, dừng ở đó — dùng khi muốn debug riêng stage `lint` hay `test`.
- **BuildKit** dựng DAG dependency → bỏ qua stage không liên quan, chạy stage độc lập song song.

**Vì sao:** ngôn ngữ biên dịch (Go/Java/C++) cần toolchain build nặng hàng trăm MB — nhưng runtime chỉ cần binary/jar. Cách cũ ("builder pattern") phải giữ 2 Dockerfile + script ngoài; multi-stage gộp vào 1 file, dễ maintain, giảm image hàng chục lần, thu hẹp attack surface.

**Cơ chế:** Docker đọc toàn bộ Dockerfile, dựng DAG các stage và dependency `COPY --from`. BuildKit chạy song song các stage không phụ thuộc nhau. Stage cuối (không có stage nào `COPY --from` nó nữa, hoặc stage được chỉ định cuối cùng) là image output. Mọi layer của stage builder không đưa vào image — chỉ file được `COPY --from` mang sang.

![[multistage.excalidraw]]

> **Ẩn dụ:** xưởng đúc (builder stage) cần lò luyện kim, khuôn, dụng cụ nặng → đúc ra cái chìa khoá (binary). Giao cho khách chỉ cần cái chìa khoá — không giao cả lò luyện kim.

| | 1-stage | Multi-stage |
|---|---|---|
| Số Dockerfile | 1 | 1 |
| Image final chứa | toolchain + binary | binary (+ runtime deps) |
| Size điển hình (Go) | ~800 MB | ~10 MB |
| Attack surface | rất lớn | nhỏ |

**Dùng / không:** bất kỳ ngôn ngữ cần bước compile/transpile (Go, Java, Rust, TypeScript). **Phản đề:** Python/Node thuần interpreter — không cần compile → multi-stage ít lợi hơn; lợi ích chủ yếu là tách stage `test` + `prod` để chắc chắn artifact test = artifact deploy.

**Làm:**
```bash
cd ~/dev/k8s-labs/01-docker-images/app/multistage

cat > main.go <<'EOF'
package main
import "fmt"
func main(){ fmt.Println("hello from tiny image") }
EOF

printf 'module hello\ngo 1.22\n' > go.mod

# 1-stage: toàn bộ Go SDK nằm trong image final
cat > one.dockerfile <<'EOF'
FROM golang:1.22
WORKDIR /app
COPY . .
RUN go build -o /app/hello .
CMD ["/app/hello"]
EOF

# multi-stage: build ở golang, chạy ở alpine
cat > multi.dockerfile <<'EOF'
FROM golang:1.22 AS build
WORKDIR /app
COPY . .
RUN go build -o /app/hello .

FROM alpine
COPY --from=build /app/hello /app/hello
CMD ["/app/hello"]
EOF

docker build -t hello:fat  -f one.dockerfile   .
docker build -t hello:slim -f multi.dockerfile .
docker images | grep hello
docker run --rm hello:slim
docker rmi hello:fat hello:slim
```

**Kết quả:**
```text
$ docker images | grep hello
hello   slim   7c9e...   30s ago    10.2MB   ← chỉ binary + alpine runtime
hello   fat    2a1b...   2m ago    829MB    ← Go SDK + toolchain toàn bộ

$ docker run --rm hello:slim
hello from tiny image
```
→ **Verify:** `hello:slim` ≈ 10 MB, `hello:fat` ≈ 800 MB+; slim vẫn chạy đúng — toolchain đã bị bỏ lại ở stage `build`.

### Biến thể Java — JDK build → JRE runtime (số liệu lab thật)

Cùng nguyên lý, đổi ngôn ngữ: **JDK** (có `javac`) để build, **JRE** để chạy.

```dockerfile
# multi.dockerfile
FROM eclipse-temurin:21-jdk AS build
WORKDIR /app
COPY Main.java .
RUN javac Main.java \
    && jar --create --file app.jar --main-class Main Main.class

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=build /app/app.jar .
CMD ["java", "-jar", "app.jar"]
```

```text
$ docker images | grep hello
hello   fat    788MB   ← eclipse-temurin:21-jdk (javac + toolchain)
hello   slim   287MB   ← 21-jre-alpine + app.jar  → cắt 501 MB
```
→ **Khác Go ở "sàn kích thước":** Go/Rust build ra binary **tĩnh** → runtime về ~10 MB (`alpine`/`scratch`). Java build ra `app.jar` **bytecode**, bắt buộc có **JVM (JRE)** để chạy → slim dừng ~200–290 MB, không thấp hơn. Multi-stage vẫn đáng (bỏ compiler + build tools + cache Maven); muốn nhỏ hơn nữa dùng `jlink` cắt JRE tùy biến, hoặc base `distroless/java`.

---

## 5. Registry — tag vs digest

**Chốt:** **tag** là nhãn di động có thể đổi lại; **digest `@sha256:`** là vân tay nội dung bất biến — prod nên pin digest.

- **Tag** (`:1.0`, `:latest`): nhãn người đặt, có thể trỏ lại image khác bất cứ lúc nào.
- **Digest** (`@sha256:...`): hash nội dung image, cố định — một digest luôn là đúng một image, không đổi được.
- `docker inspect --format '{{index .RepoDigests 0}}'` — xem digest của image đã pull.

**Vì sao:** deploy dùng `:latest` → không biết đang chạy build nào, rollback vô nghĩa (bản cũ vẫn trỏ cùng tag). Ai đó push `:1.0` lên lại với code mới → tất cả node pull cùng tag nhưng ra image khác nhau. Pin digest → mỗi build có vân tay duy nhất, rollback xác định.

**Cơ chế:** registry lưu manifest của image kèm `sha256` của toàn bộ nội dung. Khi push, registry tính digest; khi pull bằng digest → registry đảm bảo bạn nhận đúng nội dung đó. Tag chỉ là pointer trong registry có thể update; digest không thể update (nội dung đổi → digest đổi).

> **Ẩn dụ:** tag = tên file `report-final.docx` (có thể overwrite); digest = hash SHA256 của nội dung file — hash khác nhau là file khác nhau, không thể giả.

**Dùng / không:** prod manifest Kubernetes → `image: repo/app@sha256:...`. Tag version cụ thể (`:v1.2.3`) chấp nhận được nếu bạn kiểm soát registry và không bao giờ overwrite tag. **Phản đề:** CI pull tool dùng `:latest` là hợp lý (muốn bản mới nhất) — miễn đó là *tool* chạy trong pipeline, không phải *artifact* deploy của bạn.

**Làm:**
```bash
docker pull nginx:alpine
docker inspect --format '{{index .RepoDigests 0}}' nginx:alpine
docker images --digests | grep nginx
# kéo bằng digest (bất biến) thay tag:
# docker pull nginx@sha256:<digest-từ-bước-trên>
```

**Kết quả:**
```text
$ docker inspect --format '{{index .RepoDigests 0}}' nginx:alpine
nginx@sha256:9c8f...b3a2   ← repo@digest: vân tay bất biến

$ docker images --digests | grep nginx
nginx   alpine   sha256:9c8f...b3a2   1e5f...   22MB
```
→ **Verify:** digest in ra chuỗi `sha256:...`; kéo cùng digest trên máy khác → nhận đúng image đó.

---

## Dọn dẹp
```bash
docker rmi hello:fat hello:slim sz-bad sz-good cachedemo waste 2>/dev/null
rm -f ~/dev/k8s-labs/01-docker-images/app/multistage/{waste,bad,good,cache,one,multi}.dockerfile \
      ~/dev/k8s-labs/01-docker-images/app/multistage/{main.go,go.mod,app.js,package.json}
```

---

## Đủ khi
① `RUN rm` layer sau không giảm size — vì sao (whiteout) · ② 3 cách giảm size (base nhỏ · gộp RUN+dọn · .dockerignore) · ③ cache vỡ tại đâu thì chạy lại từ đó — vì sao tách `COPY` dep giúp cache · ④ multi-stage tách builder/runtime: `FROM … AS`, `COPY --from`, `--target` · ⑤ tag vs digest — vì sao prod pin digest.

## Recall
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

1. `RUN rm /big` ở layer sau có làm image nhỏ lại không? Vì sao?
2. Instruction nào tạo content layer? Cái nào chỉ là metadata?
3. Vì sao gộp `apt-get install` + `remove` + `clean` vào **một** `RUN`?
4. Cache của `RUN apt-get update` có tự làm mới khi có bản mới không? Cách ép?
5. Cache vỡ tại bước N → điều gì xảy ra với bước N+1, N+2?
6. Multi-stage khác "builder pattern" 2-Dockerfile ở đâu?
7. `COPY --from=build` làm gì? Stage nào trở thành image cuối?
8. `docker build --target lint` làm gì?
9. Tag khác digest thế nào? Prod nên pin cái nào?

### Đáp án

1. Không — *whiteout* chỉ che file trong union FS; file vẫn nằm ở layer cũ, vẫn tính size. Muốn nhỏ: xóa trong **cùng** `RUN` đã tạo, hoặc multi-stage.
2. `RUN` / `COPY` / `ADD` tạo content layer. `ENV`/`EXPOSE`/`USER`/`CMD` chỉ metadata, không tạo layer.
3. Để add + dọn xảy ra **trong cùng một layer** → rác không kẹt lại layer cũ → size giảm thật.
4. Không — Docker chỉ so chuỗi lệnh, không so kết quả. Ép làm mới: `docker build --no-cache`.
5. Mọi bước từ N trở xuống phải chạy lại — kể cả chúng không đổi. Cache bị vô hiệu hoá theo chiều xuống.
6. Builder pattern: 2 Dockerfile riêng + script ngoài, phải tự quản. Multi-stage: 1 file nhiều `FROM … AS`, gọn, có `--target`, BuildKit tự tối hoá.
7. `COPY --from=<stage>` lấy artifact từ stage trước sang stage sau. **Chỉ stage cuối** thành image; các stage khác bị bỏ (trừ file được COPY ra).
8. Chỉ build tới stage `lint` (kèm các stage nó phụ thuộc), bỏ qua stage không liên quan.
9. Tag = nhãn di động (đổi được, có thể trỏ image khác). Digest `@sha256:` = vân tay nội dung bất biến. Prod pin **digest** (hoặc tag version cụ thể không bao giờ overwrite), tránh `:latest`.

---

## Bắc cầu sang Kubernetes
image nhỏ/sạch → Pod pull nhanh, attack surface nhỏ · multi-stage = chuẩn build image catalog · **pin digest** `image: repo/app@sha256:...` trong manifest = cách chắc chắn nhất, khớp tinh thần GitOps immutable.

---

## Nguồn
- Chương trước: [01 · Docker first app](01-first-app.md) · tự kiểm: [notes.md](notes.md).
