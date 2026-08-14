# Kubernetes — ConfigMap & Secret: tách cấu hình khỏi image

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [k8s-configmap-secret.md](k8s-configmap-secret.md).

## Vì sao tách config khỏi image

<details>
<summary>1. Vì sao không bake cứng config vào Docker image?</summary>

Config đổi theo môi trường (dev/staging/prod) → bake cứng thì phải build lại image mỗi env → vi phạm
"một image, nhiều môi trường" (12-Factor, factor III). Thêm nữa K8s schedule Pod bất kỳ node nào và
scale nhiều bản → không thể drop file config lên host. ConfigMap/Secret lưu tập trung trong cluster,
inject lúc Pod khởi động.
</details>

## Tạo ConfigMap

<details>
<summary>2. <code>--from-file</code> vs <code>--from-env-file</code> khác gì ở <code>data</code>? (thực chạy)</summary>

`--from-env-file`: mỗi dòng `KEY=value` = một key riêng trong `data` (lab thật: `ENEMIES`, `LIVES: "3"`,
`CHEAT_LEVEL` — 3 key). `--from-file`: **tên file = 1 key duy nhất**, toàn bộ nội dung file = value blob
(lab thật: 1 key `game-config.env` chứa khối `|` multiline). Chú ý mọi value ConfigMap là **string** →
`LIVES` phải bọc nháy `"3"`.
</details>

<details>
<summary>3. 4 cách tạo ConfigMap, dùng cách nào khi nào?</summary>

YAML manifest (production, check git) · `--from-env-file` (có sẵn `.env`, tách từng key) · `--from-file`
(config file nguyên khối như nginx.conf → 1 blob) · `--from-literal` (dev/test nhanh, KHÔNG track được
git → cluster chết là mất giá trị cũ).
</details>

## Env var vs volume mount

<details>
<summary>4. Volume mount từ ConfigMap có tự cập nhật không? Env var thì sao? (thực chạy)</summary>

Volume mount **tự cập nhật** ~30–60s khi ConfigMap đổi, Pod KHÔNG restart. Env var **đóng băng** lúc
container start — phải restart mới đọc lại. Lab thật: `kubectl patch cm ... ENEMIES=zombies` → file volume
`cm-vol` thành `zombies` sau ~1 phút, nhưng `printenv ENEMIES` trong `cm-env` vẫn `aliens`.
</details>

<details>
<summary>5. Cơ chế atomic swap của volume ConfigMap là gì? (chi tiết ..data)</summary>

Mount là **symlink 3 lớp**: `ENEMIES -> ..data/ENEMIES`; `..data -> ..2026_08_13_08_11_19.xxx/` (thư mục
timestamp); file thật trong thư mục timestamp. Khi đổi, kubelet tạo thư mục timestamp **MỚI** với toàn bộ
giá trị mới rồi **atomic swap** symlink `..data` (một `rename`) → app không bao giờ đọc trúng trạng thái
half-written. Lab thật: `..data` đổi từ `..08_11_19` sang `..08_13_58`.
</details>

<details>
<summary>6. "Env bất biến" — có thật là mãi mãi không?</summary>

Không. Bất biến chỉ trong **vòng đời một container**. Lab thật: `command: "... && sleep 3600"` → sau 1h
`sleep` hết, container exit 0, `restartPolicy: Always` restart → env var đọc LẠI ConfigMap. Nếu ConfigMap
đã đổi giữa chừng, container mới thấy giá trị mới. Restart = start mới = re-read.
</details>

<details>
<summary>7. Cú pháp đọc 1 key vs nạp toàn bộ ConfigMap vào env?</summary>

Một key: `env: - name: VAR` → `valueFrom: configMapKeyRef: {name: <cm>, key: <k>}` (tên biến tự đặt).
Toàn bộ: `envFrom: - configMapRef: {name: <cm>}` (tên biến = tên key). Key có dấu chấm (`a.b.c`) không
dùng được làm tên env → phải volume mount hoặc đổi tên.
</details>

## Secret

<details>
<summary>8. Base64 có phải mã hoá không? Hệ quả khi commit Secret manifest vào git thường? (thực chạy)</summary>

**Không** — base64 chỉ là encoding biểu diễn, `base64 --decode` đảo ngược ngay không cần key (lab thật:
`bXlwYXNzd29yZA==` → `mypassword` tức thì; `czNjcjN0` → `s3cr3t`). Commit Secret thô vào git = ai clone
repo cũng đọc được. Dùng SealedSecrets/Vault/external-secrets cho GitOps.
</details>

<details>
<summary>9. Secret lưu ở đâu trên node? Trên master? K8s có gửi tới mọi node không?</summary>

Trên node: **tmpfs** (RAM, không ghi disk). Trên master: **etcd** (base64, cần bật encryption-at-rest
riêng — mặc định không mã hoá). K8s chỉ gửi Secret tới **node có Pod đang cần** → giảm attack surface.
</details>

<details>
<summary>10. <code>secretKeyRef</code> vs <code>configMapKeyRef</code>? Container nhận base64 hay plaintext?</summary>

Chỉ khác tên trường (`secretKeyRef` thay `configMapKeyRef`). K8s **tự decode base64** trước khi inject →
container nhận **plaintext** cả qua env var lẫn volume file. Lab thật: `log` `DB_PASS=s3cr3t`, file
`/etc/db-passwords/db-password` = `s3cr3t` (không phải base64).
</details>

## Bắc cầu sang Kubernetes production

<details>
<summary>11. ConfigMap/Secret dùng ở cụm thật thế nào?</summary>

- Một image build sẵn → ConfigMap/Secret khác nhau cho từng env (dev/staging/prod), không build lại image.
- Đổi config → cập nhật ConfigMap + `kubectl rollout restart deployment` (env var) hoặc chờ volume sync.
- Secret KHÔNG commit thô: SealedSecrets (encrypt bằng public key cluster) / Vault / external-secrets.
- `imagePullPolicy`: image không tag (`:latest`) → `Always`; pin tag/digest → `IfNotPresent` (nối module 01).

| ConfigMap/Secret (module này) | Kubernetes production |
|---|---|
| tách config khỏi image | 1 image, N môi trường (12-Factor) |
| volume mount hot-reload | đổi config không cần rebuild |
| base64 ≠ mã hoá | SealedSecrets/Vault cho GitOps |
| Secret chỉ tới node cần + tmpfs | giảm attack surface |
</details>

## Ôn tập — đào sâu

<details>
<summary>12. Đổi ConfigMap mà inject qua env var — làm sao ép Pod đọc giá trị mới? (<code>kubectl rollout restart</code>)</summary>

Env var **đóng băng lúc container start** (kernel không sửa được biến môi trường của process đang chạy). Muốn
env đọc giá trị mới → phải có **Pod mới**. Cách chuẩn, không downtime:

```
kubectl edit configmap game-config              # sửa giá trị
kubectl rollout restart deployment/game         # ép thay toàn bộ Pod bằng Pod mới sạch
kubectl rollout status deployment/game          # chờ rolling xong
```

`rollout restart` = **rolling update giả**: kubectl chèn một annotation timestamp
(`kubectl.kubernetes.io/restartedAt`) vào pod-template → Deployment nghĩ "có bản mới" → đẻ RS mới, thay Pod
từng cái một (luôn còn Pod phục vụ). Pod mới start → đọc lại ConfigMap → có env mới. Chi tiết cơ chế: notes
module 04, câu 17.

Đối chiếu với **volume mount**: KHÔNG cần restart — kubelet tự sync file sau ~30–60s (atomic swap symlink,
câu 5). Chỉ **env var** mới cần `rollout restart`.
</details>

