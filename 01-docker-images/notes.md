# Docker — Multi-stage build, tối ưu image & registry

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [02-multistage-registry.md](02-multistage-registry.md).

## Layer & image size

<details>
<summary>1. <code>RUN rm /big</code> ở layer sau có làm image nhỏ lại không? Vì sao?</summary>

**Không.** Union filesystem chỉ ghi một **whiteout marker** (`.wh.big`) để che file khỏi view của
container — layer cũ chứa 50 MB bytes vẫn nằm nguyên. `docker history` cho thấy layer `dd` vẫn
**52.4 MB**, layer `rm` = **0B**; `docker images` báo DISK USAGE ~67 MB dù `/big` đã bị xóa.

Muốn nhỏ thật: xóa **trong cùng** `RUN` đã tạo file, hoặc bỏ hẳn layer bằng multi-stage.
</details>

<details>
<summary>2. Instruction nào tạo content layer? Cái nào chỉ là metadata?</summary>

`RUN`, `COPY`, `ADD` tạo **content layer** (có bytes, tính vào size). `ENV`, `EXPOSE`, `USER`,
`WORKDIR`, `CMD`, `ENTRYPOINT`, `LABEL` chỉ là **metadata** — 0B, không tạo layer.
</details>

## Giảm size

<details>
<summary>3. Vì sao phải gộp <code>apt-get install</code> + <code>remove</code> + <code>clean</code> vào MỘT <code>RUN</code>?</summary>

Layer **bất biến**: Docker "chốt sổ" một layer sau khi `RUN` kết thúc. Cài ở `RUN` này, dọn ở `RUN`
khác thì phần đã cài (46.8 MB curl + cache apt) đã bị **niêm phong** vào layer trước; layer dọn chỉ
ghi whiteout **238kB**, không gỡ được bytes cũ. Gộp 1 `RUN` → cài + dọn xảy ra **trước khi** layer
đóng → chỉ phần net (25 MB) được ghi.

Lab thật: `sz-bad` (2 RUN) = 209 MB vs `sz-good` (1 RUN) = 170 MB → chênh **39 MB**.

Ba đòn giảm size: base tối giản (`alpine`/`slim`/`distroless`) · gộp add+dọn 1 RUN · `.dockerignore`
cắt build context.
</details>

## Build-cache

<details>
<summary>4. Cache vỡ ở bước N thì bước N+1, N+2 ra sao? Vì sao tách <code>COPY</code> dep giúp cache?</summary>

Vỡ ở N → **mọi bước từ N trở xuống rebuild**, kể cả không đổi (cache vô hiệu theo chiều xuống). Nên
đặt cái **ít đổi lên trên, hay đổi xuống dưới**: `COPY package.json` + `RUN npm install` **trước**
`COPY . .`. Khi chỉ sửa source, `package.json` không đổi → `RUN npm install` in **CACHED**, không
cài lại dep. Cache của `COPY` tính theo **checksum nội dung file**; của `RUN` tính theo **chuỗi lệnh**.
</details>

<details>
<summary>5. Cache của <code>RUN apt-get update</code> có tự làm mới khi repo có bản mới không? Cách ép?</summary>

**Không.** Docker chỉ so **chuỗi lệnh**, không so kết quả thật → dùng cache cũ kể cả repo đã có bản
mới. Ép làm mới: `docker build --no-cache` (hoặc đổi lệnh để vỡ hash).
</details>

## Immutable & tag

<details>
<summary>6. Image là immutable, vậy sao chạy <code>docker build -t app .</code> hai lần vẫn được?</summary>

"Immutable" chỉ áp cho **layer** và **image** (định danh bằng `sha256`), KHÔNG áp cho **tag**. Tag
(`app:latest`) chỉ là **con trỏ di động**. `docker build` không sửa image cũ — nó **đúc image mới**
(hoặc tái dùng layer cache) rồi **gỡ tag khỏi image cũ, dán sang image mới**. Image cũ không đổi một
byte; nó chỉ **mất tag** → thành dangling `<none>` (có thể bị GC).

Ba tầng: **tag** = con trỏ (mutable) → **image manifest** = `sha256` (immutable) → **layer** =
`sha256(nội dung)` (immutable).
</details>

## Multi-stage

<details>
<summary>7. <code>COPY --from=build</code> làm gì? Stage nào trở thành image cuối?</summary>

`FROM golang:1.22 AS build` đặt **tên** cho stage; `COPY --from=build /path ...` lấy artifact từ
stage đó sang stage hiện tại. **Chỉ stage cuối** thành image final — mọi stage builder bị vứt, trừ
đúng file được `COPY --from` mang sang. Vd Go: 1-stage ~800 MB (cả Go SDK) vs multi-stage ~10 MB
(chỉ binary + alpine).
</details>

<details>
<summary>8. <code>docker build --target lint</code> làm gì?</summary>

Build **dừng ở stage tên `lint`** (kèm các stage nó phụ thuộc), bỏ qua stage sau. Dùng để chạy
riêng stage `test`/`lint` mà không build tới image cuối. BuildKit dựng DAG các stage → chạy song
song stage độc lập, bỏ qua stage không liên quan.
</details>

<details>
<summary>9. Multi-stage Java: vì sao image slim vẫn ~287 MB, không xuống ~10 MB như Go?</summary>

Go/Rust build ra binary **tĩnh** (tự chứa) → runtime chỉ cần `alpine`/`scratch` (~10 MB). Java build
ra `app.jar` là **bytecode**, bắt buộc có **JVM (JRE)** để chạy → sàn kích thước là cỡ JRE. Lab thật:
`hello:fat` (JDK) 788 MB → `hello:slim` (JRE-alpine + jar) 287 MB, **cắt 501 MB**. Vẫn rất đáng (bỏ
`javac` + build tools); muốn nhỏ hơn dùng `jlink` cắt JRE tùy biến, hoặc base `distroless/java`.
</details>

## Registry — tag vs digest

<details>
<summary>10. Tag khác digest thế nào? Prod nên pin cái nào?</summary>

**Tag** (`:1.0`, `:latest`) = nhãn người đặt, **trỏ lại image khác lúc nào cũng được**. **Digest**
(`@sha256:...`) = hash nội dung image, **bất biến** — một digest luôn đúng một image. Prod pin
**digest**: `image: repo/app@sha256:...` → mọi node kéo về đúng một image, rollback xác định. Tránh
`:latest` cho artifact deploy. Xem digest: `docker inspect --format '{{index .RepoDigests 0}}' <img>`.
</details>

<details>
<summary>11. <code>nginx:alpine</code> và <code>nginx:latest</code> — có chắc là cùng một image không?</summary>

**Không.** Lab thật: `:alpine` → digest `sha256:4a73073b...` (94 MB), `:latest` → digest
`sha256:5a88c9c4...` (258 MB). Hai tag khác nhau trỏ **hai image khác nhau** (digest & size khác).
Đây là bằng chứng tag chỉ là con trỏ; digest mới định danh nội dung thật.
</details>

## Bắc cầu sang Kubernetes

<details>
<summary>12. Các bài học này dùng lại ở Kubernetes thế nào?</summary>

- Image nhỏ/sạch → Pod **pull nhanh**, **attack surface nhỏ**.
- Multi-stage = chuẩn build image cho catalog (tách builder/runtime).
- **Pin digest** trong manifest: `image: repo/app@sha256:...` — cách chắc chắn nhất, khớp tinh thần
 **GitOps immutable** (mọi thay đổi qua Git, môi trường xác định).

| Docker/build | Kubernetes |
|---|---|
| image nhỏ, layer cache | Pod pull nhanh, node ít tải |
| multi-stage builder/runtime | build catalog image |
| tag di động | tránh `:latest` trong manifest |
| digest `@sha256:` | `image: repo/app@sha256:...` (immutable deploy) |
</details>
